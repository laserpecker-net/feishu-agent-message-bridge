# 设计稿：唯一大脑 + 任务账本 + 记忆库（2026-11-05）

> 本稿只写设计，不改代码。目标是把"通道已经很稳、智能没跟上"这件事拆成三步可交付的改动。
> 一次只做一步，每步独立可回滚、有可复跑的判据。

## 0. 一句话

**bridge 只做通道和账本；大脑只有一个（DSH 消费者）；记忆一个真身（jsonl）+ 一个可重建索引（SQLite FTS5）；回合制，跨回合的状态只存在账本里。**

## 1. 决策记录（2026-11-05 本人拍板）

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | **收敛大脑**：删掉内置 Cursor 分析器和 codex 入口，DSH 是唯一大脑 | 本人这台机器才有 Cursor，同事只有 DSH；三套并行 = 同一问题三种能力边界 |
| D2 | **不做会话续跑**：飞书是触发-反馈，每条消息一次冷启动 run | 回合制天然适配；跨回合的连贯性放在 bridge 的任务账本，不放在 agent 内存里 |
| D3 | **记忆库**：jsonl 是真身、SQLite FTS5 是派生索引、md 是人读视图；不做 embedding；归纳层只出候选，升格要本人一句话 | 一条事实一个答案；DB 迁移不能吃掉记忆（CONTRACTS 已立契约） |

## 2. 现状证据（为什么现在必须做）

1. **三个大脑并行，而且都在真机上出过话。**
   - 内置 Cursor 分析器：`bridge/agent.py`（797 行）+ `bridge/service.py:930 analyst()` 线程；
   - `dsh-consumer.py`：本机 launchd `io.feishu-agent.dsh-consumer`（pid 8629，consumer `dsh-main`，15 claims）；
   - `agentkit-consumer.py`：旧实验留下 `agentkit-cursor`（46 claims）、`codex-main`、`cursor-main` 等注册记录。
2. **两条路的权限语义不同。**
   - 内置：`cursor-agent --trust --force --sandbox disabled`（[bridge/agent.py:635](../../../bridge/agent.py)）+ `Read(/**)` + `Shell(*)` = 整盘可读写、任意命令，仅禁 `Delete/Task/Mcp`；
   - DSH 消费者：`workspace-write` + `approval never` + 禁子代理/MCP = 被限制在工作区。
   同一个问题走哪条路，能读到的东西不一样 —— 这是实质差异，不是风格差异。
3. **内置这条路对同事是装不上的。**
   `install.py:750` 硬要求 `cursor-agent`，缺失直接拒绝安装；三份文档都在教绕过：
   `INSTALL-MACOS.md:27`、`INSTALL-WINDOWS.md:193`、`docs/onboard-windows-dsh-colleague.md:735`
   的 `CURSOR_BINARY=/usr/bin/true`。**给所有人留了一个"显式关掉内置大脑"的逃生门，说明它本来就不该是默认。**
4. **记忆是全量注入，没有检索、没有归因。**
   `teachings.jsonl` 只有 `kind/text/expose`（[agent_prompt.py:22](../../../agent_prompt.py) `TEACHING_MAX_CHARS=4000`），
   超出预算就静默丢条；没有"这条被用过几次、用对还是用错"。
5. **没有"活"，只有"消息"。**
   schema 只有 `meta/groups/scans/inbox/outbox/consumers/events`；失败只能进 `needs_attention`
   （当前 dead events = 21），无法复盘、无法复跑。

## 3. L0 收敛大脑（约 1 天）

### 3.1 目标

- 唯一大脑 = DSH。本机已在跑 `dsh-consumer.py`，**不需要换**；要删的是 bridge 里那条 Cursor 路。
- 删掉 Cursor 与 Codex 两条入口，`external` 变成唯一模式（`external enable/disable` 开关一并删——`disable` 的唯一作用就是退回内置大脑）。
- `agentkit` 保持现状：它现在是"委托 legacy 入口"的监督层（`agentkit/adapters/legacy.py` 文档开头自己写着"委托既有模块，不重写策略"），**不产生第二个大脑**，本轮不改它的架构。

### 3.2 改动清单

**a. 搬走纯函数，然后删 Cursor 适配器**

新建 `bridge/reply.py`，把 `bridge/agent.py` 里与 Cursor 无关的四个东西搬过去：

| 东西 | 现位置 | 现有引用点 |
|---|---|---|
| `render_reply` | agent.py:191 | service:12、cli:979/1238、gateway:286 |
| `resolve_signature` | agent.py:172 | cli:834 |
| `owner_name_at` | agent.py:186 | cli:979/1238、gateway:286 |
| `MAX_OUTPUT` | agent.py:47 | agentkit/adapters/legacy.py |

删 `bridge/agent.py` 的 `Agent` 类、`ALLOW`/`DENY`、argv、`finalize`/超时处理、流解析。

**b. 删 analysis 状态机**（19 处，`bridge/service.py` + `bridge/store.py`）

- `service.analyst()` 线程与 `self.agent`；
- `claim_message('analysis')` / `complete_analysis` / `retry_message` 的 analysis 分支；
- `UNFINISHED_INBOX_STATES` 里的 `analysis_queued/analysis_running`、`recover()` 的 analysis 那段；
- `details()` 里的 `state = 'ignored' if reason else 'analysis_queued'` → external 模式下只剩 `external_queued`。

**c. 删 codex 入口**

`codex-consumer.py`、`agentkit/adapters/legacy.py` 的 codex adapter、`tests/test_codex_consumer.py`、
`bridge/release.py` 的 `CONSUMER_FILES`（去掉 codex-consumer.py）。

**d. install 不再要求 cursor**

删 `install.py:742` 起的 cursor preflight、`CURSOR_BINARY` 环境变量、`config['cursor_binary']` 键；
删三份文档里的逃生门段落（INSTALL-MACOS:27、INSTALL-WINDOWS:193、onboarding:735）。

### 3.3 失败语义（这是 L0 最重要的取舍）

消费者不在线时：事件留在 `pending`，`doctor` 报 **`consumer_offline`（fail 级）**，`events stats` 的 pending 会显式增长。

**不再有"内置兜底回答"。** 这是有意的（AGENTS.md「静默降级不是修复」）：宁可显式告诉本人"没人接"，也不让两条能力边界不同的路给出两种答案。漏答提醒仍然走 `attention` 事件 + 私聊闸门，本人会知道有人在等他。

### 3.4 判据

1. `grep -rn "cursor_binary\|CURSOR_BINARY\|cursor-agent\|analysis_queued" --include=*.py --include=*.md .`
   → 只允许出现在 `docs/HISTORY.md` 的"已删除"说明里。
2. `python3 -m pytest -q` 全绿（预计净减 60–100 条 cursor/analysis 相关测试）。
3. 真机：`launchctl bootout` 消费者 → 私聊发一条「问：现在能读什么」→ inbox 停在 `external_queued`、
   `doctor` 报 `consumer_offline`；重启消费者 → 该消息被自动处理并回复。

## 4. L1 任务账本（约 2–3 天）

### 4.1 为什么需要它（不是为了续跑）

回合制下"一个活分三步"的连贯性来自**账本**：每次触发都是冷启动，但启动时读到"这个活做到第几步、产物在哪、上次结论是什么"。等价于续跑，但没有会话、不烧 token、可随时换大脑、可审计。

### 4.2 数据模型

```sql
CREATE TABLE tasks (
  id          TEXT PRIMARY KEY,   -- tsk_<uuid>
  chat_id     TEXT NOT NULL,      -- 归属会话（回复发回这里）
  title       TEXT NOT NULL,      -- 一句话目标
  state       TEXT NOT NULL,      -- open|running|blocked|done|failed|cancelled
  messages    TEXT NOT NULL,      -- json: 触发过它的 inbox 消息 id
  steps       TEXT NOT NULL,      -- json: [{n, goal, run_id, outcome, artifact_ids, at_ms}]
  artifacts   TEXT NOT NULL,      -- json: [{path, kind, sha256}]
  open_question TEXT,             -- 上次向本人确认的问题（对齐 ask_owner）
  created_at  REAL, updated_at REAL
);
```

- `events` 增加 `kind='task'`，payload = `{task_id, step, goal}`；
- 消费者每轮：claim → 读 task 账本 → 干这一步 → 写回 step/artifact/state → 提交回复；
- 仍然一消息一事件，task 只是账本，不是进程。

### 4.3 与现有 followup 机制的关系

`ask_owner` + `owner_answer` 已经是"跨回合问一句"的现成机制：task 进 `blocked` 时把问题发私聊，
本人引用回复 → 重放这条消息时带 `owner_answer` → 同一条 task 继续。**不新造第二套问答回路。**

### 4.4 失败语义

- 单步失败：`steps[].outcome=failed` + 原因；task 进 `failed`，保留可复盘证据（不再只是"死信+1"）；
- 超时/进程被杀：`running` 超过 lease → 回 `open`、attempts+1；超过 3 次 → `blocked` 并 notify 本人；
- 账本写失败：整步回滚（事务），事件重试，不产生半个 step。

### 4.5 判据

1. 一个真活跨 3 条消息完成（例：读 3 个 Excel → 汇总 → 发到群里），`tasks.steps` 有 3 条、`artifacts` 有 2 个。
2. 中途 kill 消费者再启动，同一个 task 从 `running` 回到 `open` 并续做，**不重复副作用**（幂等键仍按 message_id + purpose）。
3. 24 小时内 dead events 不再增长（失败都有状态可查）。

## 5. L2 记忆库（约 3–4 天）

### 5.1 真身：`teachings.jsonl`（schema 2，只追加）

```json
{"schema": 2, "id": "t-0007", "kind": "principle", "text": "称呼我为宝哥", "expose": "say",
 "status": "active", "confidence": 0.9,
 "source": {"message_id": "om_…", "chat_id": "oc_…", "at_ms": 1793865242000, "quote": "以后不要称呼我为刘总…"},
 "evidence": {"used": 7, "good": 6, "bad": 1, "last_used_at": 1793866561000},
 "created_at": 1793865243000, "verb": "add|disable|confirm"}
```

- 旧行没有新字段 → 读时补默认值，**不迁移、不改写历史**；
- `verb` 让"撤回/升格"也是追加（现有 `teach revert` 已经是这个模式）；
- `teach.json` 仍是开关 + `rev`。

### 5.2 派生索引：`memory.sqlite3`（可删、可重建）

```sql
entries(id TEXT PRIMARY KEY, kind TEXT, text TEXT, status TEXT, confidence REAL, updated_at REAL);
entries_fts USING fts5(text, content='entries', content_rowid='rowid');
ticker(stamp TEXT);   -- teachings.jsonl 的 sha256 + 行数
```

- 启动时比对 `ticker.stamp`，不一致 → 整表重建（真身是 jsonl）；
- 索引损坏 → 删掉重建，**绝不把"索引读不出来"当成"记忆没了"**（现有 `teach status` 的 `bad_lines` 语义保持）。

### 5.3 检索：替换"全量注入"

`bridge/memory.py::retrieve(text, kinds=...)`：

1. **必修**：`principle` + `style`（active、confidence ≥ 0.5，最多 20 条）；
2. **选修**：拿消息正文做 FTS5 匹配 → `fact` / `pointer` / `lesson` top 8；
3. 排序：`bm25 × confidence × 新近度`；总长 ≤ 4000 字（沿用 `TEACHING_MAX_CHARS`）；
4. 返回 `(section, used_ids)`，`used_ids` 写进这一轮账。

渲染格式不变（仍是 `agent_prompt.teaching_section` 的那一段，六个渲染点共用同一段文本的约束保留），
只是内容从"全量"变成"检索结果"。

### 5.4 归因：`turns.jsonl`

```json
{"turn_id": "…", "message_id": "om_…", "chat_id": "oc_…", "kind": "answer|task|ask_owner",
 "memory_ids": ["t-0003", "t-0007"], "outcome": "sent|failed|timeout",
 "reply_message_id": "om_…", "at_ms": 1793866561000}
```

反馈信号（全部来自已有机制，不新造入口）：

| 信号 | 来源 | 记法 |
|---|---|---|
| 本人引用回复 `reply_message_id` 且含「对/不对/不是/错了」 | 现有 quote-reply 解析 | good / bad |
| 本人下一句被判成 `complaint` | 现有 `teach.classify` | bad + 生成 `lesson` 候选 |
| 任务 `done` / `failed` | L1 账本 | good / bad |
| 没有反馈 | —— | 不加不减（"没说就是好"是自欺） |

### 5.5 归纳层（只出候选）

- 触发：同一类 `bad` ≥ 2 次，或同一任务连续失败；
- 产出：一条 `lesson` 候选，`status=pending`，走**现有**私聊确认（回「记」升格为 principle/style 的 active，回「不记」→ disabled）；
- `复盘` 命令：候选列表 + 每条记忆的战绩（used/good/bad）+ 长期未使用（used=0 且 >30 天）的清单。

### 5.6 人读视图：`教材.md`

每次变更后重写（真身仍是 jsonl），供本人肉眼检查、给同事看、git diff。手改会被下次导出覆盖 —— 这一点写在文件头。

### 5.7 判据

1. 检索：构造 200 条记忆，给定一条消息，`retrieve` 命中预期的 fact/pointer（测试钉住）。
2. 不再全量注入：prompt 里教材段 ≤ 4000 字，且只含检索结果。
3. 归因闭环：一次"被骂" → 1 条 `lesson` 候选 → 回「记」升格 → 下一次回答的 prompt 里出现它。
4. 索引可重建：删 `memory.sqlite3` → 启动自动重建，`教材清单` 条数不变。

## 6. 迁移顺序（每步独立可回滚）

| 步 | 内容 | 判据 |
|---|---|---|
| L0-a | 新建 `bridge/reply.py`，所有引用点改指向 | 测试绿，无行为变化 |
| L0-b | 删 analyst 线程 + analysis 状态机 + `external` 开关 | 测试绿；真机停消费者 → `consumer_offline` |
| L0-c | 删 codex 入口 + install 的 cursor 要求 + 三份文档逃生门 | `grep` 零残留 |
| L1 | `tasks` 表 + `events.kind='task'`（先只对「派活」生效） | §4.5 三条 |
| L2-a | jsonl schema 2 + FTS5 索引 + 检索（**不动归纳**） | §5.7 第 1、2、4 条 |
| L2-b | `turns.jsonl` 归因 + `lesson` 候选 + `复盘` | §5.7 第 3 条 |

**建议先做 L2-a**（不动归纳策略）：只改"怎么把记忆给到 agent"，跑一周真机数据，看清哪些记忆真被用上、哪些一次没用，再定归纳规则。风险最小，见效最快。

## 7. 明确不做的

- 向量检索 / embedding（没有稳定的本地嵌入，也不该把私聊记忆发给外部服务）；
- 自动升格（归纳只出候选，升格必须本人一句话）；
- 会话续跑 / `agentkit` 的 `resume_key`（D2：飞书是回合制）；
- 把记忆放进 SQLite 当真身（违反 CONTRACTS「教材不进 SQLite、不进 git」）；
- 在 bridge 里再写第二个大脑。

## 8. 未验证项（诚实清单）

1. `agentkit` 当前是"委托 legacy 入口"的监督层；L0 之后它仍然委托 `dsh-consumer.py`。把它改成原生 adapter（删掉 legacy 委托）不在本轮，记为后续待办。
2. Windows 侧的消费者切换只有代码路径验证，没有真机（与 `AGENTKIT.md` §8.2 同一批未验证项）。
3. FTS5 在 Python 3.9.6 + SQLite 3.51 上已在本机验证可用；Windows 侧 Python 自带的 SQLite 是否带 FTS5 需要在那边跑一次 `create virtual table … using fts5` 才能算数。
