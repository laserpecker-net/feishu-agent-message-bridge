# S1 实施计划：出口唯一（outbox v8）

> 设计依据：[design-s1-outbox-v8.md](design-s1-outbox-v8.md) v1.6（唯一 DDL 来源）
> 评审记录：[review-s1-hy4.md](review-s1-hy4.md)（四轮，20+ 条阻断全部关闭）
> 状态：**代码三件套已完成（PR-1/2/3），未部署**。生产库仍是 v7、线上跑的是旧 release。
> 剩两件事：①部署（停机 + 迁移 + 换 release，见 §6）②真机验收（需主人 @ 机器人配合）。

---

## 0. 完成判据（可复跑，不写"已验证"）

| # | 判据 | 命令 / 证据 |
|---|---|---|
| 1 | 全仓测试通过 | `python3 -m pytest -q`（**1109 条**，基线数字先记下来） |
| 2 | 脱敏门禁 | `python3 -m pytest tests/test_repo_hygiene.py -q` → 4 passed |
| 3 | 旧结构零残留 | `grep -rn "o\.id *= *i\.id" bridge/ tools/ tests/` 无输出；`grep -rn "get('original_id')" bridge/` 无输出 |
| 4 | 迁移可复跑 | `python3 tools/migrate-outbox-v8.py --dry-run --db <v7副本>` 与实跑，贴行数/状态分布对比 |
| 5 | 真机：两句按序 | `events say` + `events reply` → 群里先「收到」后「做完了」，贴两条 mid |
| 6 | 真机：撤回生效 | `events retract --message-id <say 的 mid>` → `transport.get_message(mid)['deleted'] == True` + `SELECT reply_id,retracted_at FROM outbox WHERE row_id=?` 两列有值 |
| 7 | 网关两端点 | Windows 开发机 `POST /v1/events/{id}/say` → 群里出现；`retract` 撤回 |

---

## 1. 三个 PR（不能更少，顺序固定）

### 第 0 步（0.5h，开工前必做）

跑两条 grep，把必改点落成 issue 清单，逐条标标识符归属：

```bash
grep -rn "o\.id *= *i\.id\|original_id\|get_outbox(\|FROM outbox\|INTO outbox\|UPDATE outbox" \
     bridge/ tools/ tests/ --include=*.py
grep -rn "outbox" tests/ --include=*.py | wc -l    # 基线：167 处 / 6 个文件
```

必须人工确认的三处（前几轮评审在这三处反复翻车）：

| 位置 | 归属 |
|---|---|
| `store.py:548` | inbox id（这条更新的是 inbox） |
| `store.py:855` | **`WHERE inbox_id=? AND kind='reply'`**（入参是消息 mid，选了 row_id 会让判重失效） |
| `store.py:919` | 同上，`kind='reply'` 过滤 |

---

### PR-1 = S1a + S1b（4–6.5 天）：结构 + 迁移 + 版本门禁，必须原子

**为什么不能拆**：下界拒绝一落地，生产 v7 库立刻启动失败；迁移脚本必须已经在同一个 PR 里。

| 序 | 动作 | 文件 |
|---|---|---|
| 1 | 新增模块级常量 `OUTBOX_V8_DDL`（建表 + `outbox_seq` 部分唯一索引 + `outbox_due` + 两条触发器） | `bridge/store.py` |
| 2 | `executescript` 改用该常量；`SCHEMA_VERSION = 8`；版本链加 `if version < 8` | `bridge/store.py:44,101-124,203-209` |
| 3 | 补**下界拒绝**：代码要 v8、库是 v7 → 中文报错「库还在 v7，先跑迁移」 | `bridge/store.py:64-68` |
| 4 | 新增 `OUTBOX_BLOCKING_STATES` / `OUTBOX_RELEASING_STATES` 常量（§4 放行全集） | `bridge/store.py:350` 附近 |
| 5 | 七个方法：`append_outbox` / `claim_outbox` / `mark_send_dispatched` / `outbox_sent` / `outbox_marked` / `outbox_retracted` / `outbox_verified` / `fail_outbox` | `bridge/store.py` |
| 6 | 按 §5.3 全量表改 40 处（主键、JOIN、级联、计数口径 `kind='reply'`） | `store.py`、`metrics.py`、`service.py` |
| 7 | `tests/` 167 处 fixture 与断言改 v8 列名 | `tests/test_{store,service,gateway,cli,hold,latency}.py` |
| 8 | `tools/migrate-outbox-v8.py`（`--dry-run/--verify/--inject-fault`，逐条 `execute` 包 `BEGIN IMMEDIATE`） | 新文件 |
| 9 | 38 条用例（含 5 条迁移、3 条门禁） | `tests/` |

**门禁**：`pytest -q` 全绿；`--dry-run` 在 v7 库副本上跑通；迁移后 `user_version=8`、
`foreign_key_check` 空、`integrity_check=ok`、`sqlite_master` 里 2 触发器 2 索引。
**回滚**：`VACUUM INTO` 备份文件替换。

---

### PR-2 = S1c（1.5 天）：sender 按行发送

| 序 | 动作 |
|---|---|
| 1 | `sender()` 拆 `row_id` / `inbox_id` / `chat_id` 三键（**禁用 `mid` 变量名**） |
| 2 | kind 分叉：`reply` → 现有 `transport.reply` + `verify_reply` 流程不变；`say` → `transport.reply`（有 reply-to）或 `send_text`（主动消息）→ `outbox_marked` |
| 3 | `retract` → 新增 `Transport.delete_message(mid)` → `outbox_retracted`（一个事务写 retract 行 + 原行 `retracted_at`） |
| 4 | `send_guard` 改 `row_id + attempt_id` |
| 5 | 真机两步 demo（判据 5） |

**门禁**：`pytest -q`；`grep -rn "mid = outgoing" bridge/service.py` 无输出。

---

### PR-3 = S1d（1.5 天）：发布路径 + CLI + 网关 + 出口门禁

| 序 | 动作 |
|---|---|
| 1 | `complete_external_reply(..., kind='reply'\|'say'\|'retract')` 按 §5.6 表分叉（判重、配额、inbox 状态、事件收尾） |
| 2 | CLI：`events say` / `events retract` 两个 handler + 参数（`--consumer` 必填，`(--event+--claim-token)` 与 `--chat` 互斥）；退出码 4 约定 |
| 3 | 网关：白名单加 `say`/`retract` + 两个 handler（同一条实现） |
| 4 | `tests/test_single_exit.py` 调用方白名单门禁（sender 唯一出口 + `notify` 例外） |
| 5 | `CONTRACTS.md` 写 `notify` 例外与跨仓约束（Windows 干活端只许调 `events say/retract`） |

**门禁**：`pytest -q`；`grep -rn "Transport\.\(reply\|send_text\|send_dm\|delete_message\)" bridge/ | grep -v transport.py` 只允许出现在白名单文件；网关路由白名单测试。

---

## 2. 已预先定掉的决策（实施中不再问人）

见设计稿 §5.7 的 6 条：`reply` 的 seq 走 `MAX+1`；`chat_id` 一律从 inbox 回填；`retracted_at`
只由 `outbox_retracted()` 写；`events say` 参数互斥；迁移遇 `inbox.chat_id IS NULL` 即中止；
多行计数一律 `WHERE kind='reply'`。

---

## 3. 已定的问题：`notify` 收编

**结论（2026-xx，主人「想好、设计好、规划好」后按建议执行）：选 A —— S1 保留为唯一例外，写进
`CONTRACTS.md`，S2 收编。** 理由是 S1 的目标是「让 new 出口收敛到 outbox」，而 `notify` 是既有的、
有独立去重状态文件（`notice.state_path`）的私聊提醒通道，收编它要连去重语义一起搬，属于 S2 的范围。
这样 S1 结束时的诚实说法是「一个出口 + 一个写在合同里的例外」，不是「绝对唯一」。

**`notify`（`cli.py:1362` 的 `Transport.send_dm`）要不要在 S1 一并收编进 outbox？**（备查）

| 选项 | 做法 | 工期 | 代价 |
|---|---|---|---|
| **A（已采纳）** | 保留为**唯一例外**，写进 `CONTRACTS.md`，S2 收编 | 9–11 人日 | S1 结束时是「一个出口 + 一个显式例外」 |
| B（未采纳） | 加 `kind='notice'` 收编（目标 open_id、去重键搬进 outbox） | +0.5～1 人日 | 多一种 kind、多一条发送分支、配额语义要重定 |

---

## 4. 风险与止损

| 风险 | 止损 |
|---|---|
| fixture 改造把 1109 条测试大面积搞红，分不清是改造错还是功能错 | 改造与功能分两个 commit；先只改 fixture 让全绿，再改功能 |
| 必改点清单继续漂（四轮评审还在冒新漏项） | 第 0 步 grep 清单落成 issue 逐条销账；`test_no_outbox_dot_id_remains` 做机器门禁 |
| 迁移写坏生产库 | 停服 + `VACUUM INTO` + `--dry-run` + 三条断言（行数/每 inbox 一行/chat_id 非空）+ 备份文件回滚 |
| 主动消息路径没人测过 | 判据 7 在 Windows 开发机跑网关 `say` → 群里出现才算完 |
| 版本门禁切换把线上桥接停掉 | S1a 与 S1b 同 PR；上线顺序：先部署含迁移脚本的版本 → 跑迁移 → 再重启 |

---

## 5. 工时总计

| 段 | 人日 |
|---|---|
| PR-1（含 fixture 改造与迁移） | 4–6.5 |
| PR-2 | 1.5 |
| PR-3 | 1.5 |
| 收尾（全仓测试 + 两端真机 + 脱敏） | 0.5–1 |
| **合计** | **9–11 人日** |

最可能超时的是 PR-1；如果第 0 步的 issue 清单超过 60 条，先停下来重新估，不要硬推。


---

## 6. 完成情况与还差的两步（截至本次会话）

| PR | 提交 | 验收 |
|---|---|---|
| PR-1 结构 + 迁移 | `6fd7031` / `68db034` / `a0b7769` | 全仓 1150 → 后 1180 passed；迁移工具 6 条拒绝/回滚路径全测 |
| PR-2 sender 按行发 | `5ab6c21` / `924c702` / `339ca74` | 1163 passed；`service.py` 无 `mid`/`original_id` 残留 |
| PR-3 CLI/网关/门禁 | `9066b67` | 1180 passed；`test_single_exit.py` 4 passed，且人为注入一行发送调用能把它变红 |

**生产库迁移已在副本上实测**（`~/Library/Application Support/FeishuPersonalAgent/state.sqlite3` 的
一致性快照 → `/tmp/prod-v7-copy.sqlite3`）：

```
迁移前：user_version=7  outbox=243  states={needs_attention:11, reply_verified:232}
迁移后：user_version=8  outbox=243  states 完全一致  kind 全 reply  seq 全 1  chat_id 无空
        foreign_key_check=[]  integrity_check=ok  触发器 2 个  索引 outbox_seq/outbox_due
```

### 第 6.1 步：部署（还没做，需要主人同意停机窗口）

> 完整步骤见 **[ops-s1-deploy.md](ops-s1-deploy.md)**（含回滚要删 `-wal`/`-shm` 的原因、两个半态的报错原文）。

线上是 `~/Library/Application Support/FeishuPersonalAgent/current` 的 release 副本，
**不是本仓库**；所以本仓库改完不影响正在跑的进程，但**新代码遇到 v7 库会拒绝启动**。
部署顺序不能反：

1. 停服务（launchd）
2. `python3 tools/migrate-outbox-v8.py --dry-run --db <生产 state.sqlite3>`，确认行数/状态分布
3. 实迁（脚本自己 `VACUUM INTO` 备份）
4. 装新 release（`install.py` 或既有升级流程），切 `current`
5. 起服务，跑 `feishu-bridge status` / `doctor --assert`，贴输出
6. 回滚：把第 3 步的备份文件换回去 + 切回旧 release

### 第 6.2 步：真机验收（需要主人配合）

① 飞书里 @ 机器人一条消息；② 消费者 `events say` 一句（群里先出现）；③ `events reply`（终回复 +
inbox 变 `completed`）；④ 对 ② 那条 mid 执行 `events retract`（飞书侧消失 +
`SELECT reply_id, retracted_at FROM outbox WHERE row_id=?` 两列有值）；⑤ 网关那条：Mac 起
`events serve`，Windows 侧带 Bearer 调 `POST /v1/events/{id}/say`。


---

## 7. S1 之后：一份外部架构研究的取舍（2026-11-17）

读了 `research/super-agent/design-review.md`（对 DSH Super-agent 官方原型的源码级对照，**该目录在
`.gitignore` 里，只在本机**）。它对 S1 本身没有推翻性意见（S1 的轴是「送达可靠」，它的轴是「省人的
注意力」），但有四条直接相关的取舍记在这里，免得下次重新论证：

### 已采纳并做完

| 建议 | 处置 |
|---|---|
| P0-0 文档漂移：`README` 写「SQLite schema4」、`CONTRACTS.md` 首段写「version 7」而正文另一处写 v8 | **已修**：版本号只在 `bridge/store.py:SCHEMA_VERSION` 一处定义，两份文档改成指向它 + 说明 v7 必须先跑迁移。判据：`grep -rn "schema4\|schema v7\|version 7" README.md CONTRACTS.md` 零命中 |

### 值得做，但排在 S1 部署与真机验收之后

| 候选 | 为什么值得 | 为什么不是现在 |
|---|---|---|
| **Disposition 行程单**（`inbox.disposition` 一列 + `feishu-bridge explain <mid>`） | 我们现在回答「这条消息为什么没变成活」要跨 `inbox`/`events`/`outbox`/`tasks` + 3 份 jsonl 拼。S1 之后 outbox 一条消息可能有多行（say/reply/retract），**这种拼接只会更贵** | S1 刚把 outbox 表迁到 v8 并改完 40 处调用点，再动 `inbox` 结构会让「一次迁移」变成「两次迁移」。放 S2 第一个任务，且复用 v8 的 `row_id/seq` 体系 |
| **thread + owner**（`events.thread_key` / `events.owner`） | 解决「每条后续消息重走全链路」与「多消费者抢事件」。这正是 B 档（人审 → 工作台干活）最缺的一块 | 属于 B 档设计范围，且必须在 P0-1（出槽并发）之后做 |
| **并发出槽**（claim 与生成解耦，人的消息可越限占一格） | 串行 245s/条 vs 600s 从创建时刻起算的死线，是当前唯一「不改就一定出事」的结构性故障 | 与 S1 无关，属于消费者运行时；B 档要并行干活，绕不过去 |
| **上下文截断要诚实**（`payload.context.truncated`） | 模型不知道自己缺了什么，就无法主动补读；现在连「缺了」都不告诉它 | 小改动但属于事件载荷契约，等 B 档一并定 |

### 明确不抄（记理由，避免回头重提）

| 不抄 | 理由 |
|---|---|
| 术语体系（Mandate / Disposition / Strength / fan-out） | 概念可以借，名字不搬——直接套到 `events/outbox/inbox` 上只会变成同义词打架 |
| 投机执行 | 官方原型本身没实现；在我们这儿等于用本人账号在没被要求时先发东西，风险 >> 收益 |
| 独立的 Watchdog Session | 官方也没做；我们的 `doctor --assert` + 租约回收 + 600s 死线已同效 |
| 存算分离/BYOM 叙事 | 官方原型就是单机（SQLite + 目录 + 进程锁），追叙事只得到复杂度 |
| `danger-full-access` + 操作者同权 | 我们的 `workspace-write` + `approval never` 更保守，改造中不退回去 |

### 一条对我们的正面确认

研究里有一条值得记住的对照：它的 `paused` 与我们的 `hold` 在细则上几乎逐条重合
（源继续记录、回复挂起、在跑的 turn 不打断、窗口顺延）。**这说明 hold 的语义是对的**，
不要因为看到别人的设计就去改它。


---

## 8. 发布前对抗自查记录（2026-11-17）

自查发现 4 个「上线会真出事」的问题，全部已修（`a15ee44`），并补了 9 条用例（1180 → **1189 passed**）：

| 问题 | 现象 | 修法 |
|---|---|---|
| say 之后第二句发不出去 | 600s 事件超时 → 事件 dead → reply 被拒，**S1 产品目标失效** | `SAY_GRACE_SECONDS=3600`：say 续租 + `expire_external` 在宽限期内不判死 |
| 迁移死锁 | 停服残留 `send_started` → 迁移永远中止（租约靠运行中进程回收） | `--park-inflight`：在途行 → `needs_attention`，绝不回 `queued` |
| 回滚留 WAL | 换库不换 `-wal` → 旧帧盖新页 → `database disk image is malformed` | runbook 写死删 `-wal`/`-shm` |
| 备份 0644 | 备份里是消息正文，比生产库（0600）宽 | `chmod 600` + 用例钉住 |

自查同时核对过、**没有**问题的：迁移事务性与三条断言、`outbox_seq` 部分唯一索引、`say/retract` 失败路径（都落 `retry_wait`→`needs_attention`，`stats` 可见）、测试没有被改到迁就实现（`assertRaises` 177→186，删除的 90 条断言全是 `get_outbox(mid)`→`get_outbox_by_inbox(mid)` 改名）。


---

## 9. 收工状态（2026-11-17）

- 代码与文档：全部合入，`python3 -m pytest -q` → **1191 passed**
- 生产：已部署（release `e4e291dbc446a099`），`doctor --assert` rc=0
- 真机验收：`say` / `reply` / `retract` 三种都在生产上跑过；`say → retract → reply` 连续链路由
  `test_retract_does_not_close_the_event_then_reply_lands` 钉住（真机上是因为先撞到 bug 才分两条事件跑的）
- 遗留（不阻塞 S1）：3 条 `needs_attention` 等主人决定是否补答；`s1-demo` 这个演示消费者记录仍在
  `consumers` 表里（无自动行为）
- 下一步：B 档（人审 → 工作台干活）；S2 已落地，见 [CONTRACTS.md](../CONTRACTS.md) 的「任务卡与审批」
