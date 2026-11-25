# HY4 对 S1 设计稿（outbox v8）的评审记录

> 被评审文档：[design-s1-outbox-v8.md](design-s1-outbox-v8.md)
> 评审方：`tencent-hy` / `hy4`（Hunyuan Hy4，1M 上下文），只读沙箱，自己读代码，不喂结论。
> **跑了两轮**，两轮都对，第二轮（深审）发现的问题更狠：
>
> | 轮次 | 触发方式 | 评审对象 | 结论 | 阻断项 |
> |---|---|---|---|---|
> | A（深审） | `subagent_hy4` 独立子代理，给了 6 个文件 | v1.2 | 不通过 | **10 条**（第 4 节） |
> | B | `dsh headless` + hy4 overlay，限定 4 个文件 | v1.2 | 不通过 | 5 条（第 2 节） |
> | C | `subagent_hy4` 第二次派发，读 store.py/service.py/metrics.py | v1.4 | 不通过 | 5 条（第 5 节） |

## 结论

**不通过**：状态机用词、主键连锁、claim 老逻辑、metrics JOIN 四处与真实代码冲突，
按稿实现必出运行时错误与语义事故。→ 已全部吸收进 v1.3，见下面「处置」。

## 评审原文

证据齐了。设计稿 §5.1 的连锁表漏了 `store.py:855,877,878,919,1384,1399,1405,1424` 以及 `service.py` 内所有 `fail_outbox/outbox_sent/outbox_verified(mid,...)` 调用点的参数语义。另外 `stats()` 的 `GROUP BY state` 会把新终态 `sent` 计入 status 计数（设计稿说"不进未完成集合"但没说 status 展示口径）。

现在输出评审。

# 结论
不通过，状态机用词、主键连锁、claim 老逻辑与 metrics JOIN 四处与真实代码冲突，按稿实现必出运行时错误与语义事故。

# 阻断项（必须改）
1. 【设计稿 §3 / store.py:350】状态集**不一致**：稿称真实集含 `dead/dropped/sending/sent/verified`，但 `UNFINISHED_OUTBOX_STATES` 实为 7 个 `queued/send_started/verifying/verify_pending/retry_wait/needs_attention/outcome_unknown`；终态代码是 `reply_verified`，稿 §5.1 SQL 却写 `sending`/`verified`，§4.1 写 `sent`。改法：全文统一为代码词汇，`sent` 作为**新增终态**显式加入「非未完成」补集，并同步 `stats()`(1414 `GROUP BY state`)、`prune_expired`(412)、`drop_pending`(383) 的白名单，以及 §5.1 SQL 的 `state IN ('queued','send_started',...)` 与 `NOT IN ('sent','reply_verified','dead','dropped')`。「沿用现有状态词汇、不新造名字」与新增 `sent` 自相矛盾，须改写。
2. 【设计稿 §5.1 / store.py:542,546,549,554,563,567,569,576,579,588,591,597,600,607,616 + 855,877-878,919,1384,1399,1405,1424】连锁表**严重漏项**：只列了 377/433/436/505/569…/607，实际 `claim_outbox` 全段、`mark_send_dispatched`、`outbox_sent/verified`、`fail_outbox`、`complete_reply`(855,877-878)、`complete_external_skip`(919)、`get_outbox`(1384)、`app_recent`(1399)、`conversation_replies`(1405)、`stats`(1424) 全用 `WHERE id=?` / `ON o.id=i.id`。改法：机械全量替换 `o.id`→`o.inbox_id`、主键参数→`row_id`；§5.1 表格须以 `grep` 全量清单为准重写，不能只列 9 处。
3. 【设计稿 §5.1 / service.py:999 + store.py:1002,1009,1012,1015,1021,1023】`mid` 语义断裂：稿只把 999 行改成 `outgoing['inbox_id']`，但 `get_message/fail_outbox/outbox_sent/outbox_verified` 的 `mid` 在 v8 必须按 `row_id` 定位 outbox、按 `inbox_id` 定位 inbox/transport。改法：sender 拆成 `row_id`（outbox 行）与 `inbox_id`（飞书原消息），`fail_outbox/…` 传 `row_id`，`get_message/transport` 传 `inbox_id`；否则 say 行会拿 `inbox_id` 去 `WHERE row_id=?` 命中空、`_live` 返回 False，发送静默失败。
4. 【设计稿 §3/§5.1 / store.py:549-554,559,584-586,610-619】claim 老逻辑对 say 语义错误：①`WHERE state IN ('queued','retry_wait') AND reply_id IS NULL AND first_send_at<=?` 把 say（发成功但 `reply_id` 语义为空/超时）刷成 `outcome_unknown`，紧接着 552-554 `UPDATE inbox SET needs_attention WHERE id IN (...)` 把**整条任务**标红；②`mode='verify' if state=='verify_pending' or reply_id is not None`：say 行一旦存了飞书 `message_id` 就会被误判为 verify 模式，走 `get_message(reply_id)+verify_reply`，`outbox_sent`(585) 还强制 `reply_id` 非空字符串。改法：claim/发送流程按 `kind` 分叉，`outcome_unknown`/`needs_attention` 的 inbox 联动只允许 `kind='reply'` 触发；`mode` 判定改为 `kind=='reply' and (state=='verify_pending' or reply_id is not None)`；say/retract 用独立 `outbox_marked`(稿 §5.1 提了却未定义) 直接置 `sent`，`reply_id` 存 message_id 仅用于 retract 对账，不参与 mode 判定。
5. 【设计稿 §5.1 / metrics.py:89,100】`JOIN o.id=i.id` 一对多后**重复计数**：一条 inbox 有 say+reply 两行即产生两行样本，`LIMIT window` 也被稀释；改法：`ON o.inbox_id=i.id AND o.kind='reply'`，或改 `WHERE EXISTS`/按 `i.id` 聚合 `MIN`，指标口径只取终回复；否则 P95/样本数全失真。

# 建议项
- §7 第 6 步 `INSERT ... SELECT id,1,'reply'...` 应取 `original_id`（v7 才是 reply 的 inbox id，877-878 写入 `id=mid, original_id=mid` 虽恒等，但稿 §1 说「id 与 original_id 对 reply 恒等」需显式断言，别依赖巧合）；`INSERT` 未列 `target_mid/body_hash` 默认值须与 v8 `NOT NULL DEFAULT ''` 对齐。
- §7 第 3/8 步触发器说明与 §7.1「触发器随表删除、重建要放 drop 之前」自相矛盾，且 rename 后旧表触发器会指向 `outbox_v7`，第 8 步 `DROP TABLE` 才真正释放名字——须写清顺序并加 `sqlite_master` 校验。
- `uuid` 由 `inbox_id+seq+kind` 派生(§5.1:172)与 v7 的 `uuid` 来源(877 行 `external_uuid`)不同，迁移后旧 uuid 保留、新行用新规则，`UNIQUE` 不冲突但幂等键语义要写清。
- §8 缺：`sent` 终态不被 `drop_pending/prune/stats/UNFINISHED` 误收、`say` 行 `reply_id` 非空不误入 verify、`outcome_unknown` 不联动 inbox、metrics 多 outbox 行只算一次、迁移后 `get_outbox/app_recent` 全量回归、retract 的 `target_mid` 与 `reply_id` 对账失败(退出码 4)、同 `inbox_id+seq` 并发 append (`BEGIN IMMEDIATE` 下 MAX+1) 竞态。

# 我核对并确认正确的部分
1. §7.1 两条实测坑（rename 带走索引/触发器名、`executescript` 先 COMMIT）与 store.py:113,117-124 的 DDL 完全对得上，结论可信。
2. §4「sender 只能取该 inbox 下 seq 最小未完成行」方向正确，`outbox_seq(inbox_id,seq)` 唯一索引(稿 §3)足以支撑 `NOT EXISTS p.seq<o.seq` 子查询。
3. §2「outbox_verified 只置 inbox completed、say/retract 不碰 inbox」与 store.py:602 现状一致，分叉点判断准确。
4. §6 单出口 grep 门禁与 AGENTS.md「两套实现=未完成重构」原则一致，方向对。
5. §7 `PRAGMA user_version=7` 前置 + `>=8`/`<=7` 双向拒启、备份+`--dry-run`+双断言，回滚/防并存设计完整。

（证据均来自所读四文件指定行；`grep store.py` 仅用于定位 §5.1 漏项，未超出只读范围。）

## 处置（v1.3 已全部吸收）

| # | HY4 的意见 | 处置 | 落在哪 |
|---|---|---|---|
| 阻断 1 | 状态集不一致：稿里 `sending`/`verified` 是编的，真实终态是 `reply_verified`；`sent` 必须显式登记 | **接受**。§3.1 改成逐字照抄 `store.py:350` 的 7 个未完成状态 + `reply_verified`；新增「`sent` 必须四处登记」表（`UNFINISHED_OUTBOX_STATES` 不加 / cleanup 不变 / `stats()` 口径 / `claim_outbox` 追加 `send_started`） | §3.1 |
| 阻断 2 | 连锁改动只列 9 处，漏了 `store.py:855,877,919,1384,1399,1405,1424` 等 | **接受**。按 `grep -n outbox bridge/store.py` 重列 **18 行清单**，含 `complete_reply`、`complete_external_skip`、`get_outbox`、`app_recent`、`conversation_replies`、`stats` | §5.3 |
| 阻断 3 | `mid` 语义断裂：`fail_outbox/outbox_*` 要 `row_id`，`get_message/transport` 要 `inbox_id` | **接受**。新增 §5.1「两个标识符」契约；sender 拆 `row_id`/`inbox_id` 并**禁止再用 `mid` 变量名**；防止"静默失败只打一行 error" | §5.1、§9 |
| 阻断 4 | claim 老逻辑对 say 语义错误：①超时会刷成 `outcome_unknown` 并把 inbox 标 `needs_attention`；②`mode` 用 `reply_id is not None` 会把 say 误判成 verify | **接受**。SQL 加 `kind='reply'`；`mode` 改成 kind 优先；新增 `outbox_marked()` 给 say/retract 直接置 `sent`（原稿提了却没定义） | §5.2 |
| 阻断 5 | `metrics.py:89` 的 `JOIN o.id=i.id` 一对多后重复计数、`LIMIT window` 被稀释 | **接受**。JOIN 加 `AND o.kind='reply'`；`stats()` 按 kind 分组 | §5.3 |
| 建议 1 | 迁移第 7 步应取 `original_id`，且要断言 `id = original_id`，不靠巧合 | **接受**。第 6 步加 `ASSERT COUNT(*) WHERE id IS NOT original_id = 0`，第 7 步用 `original_id` | §7 |
| 建议 2 | 触发器/rename 顺序说明自相矛盾 | **接受**。§7.1 补「第 3 步删名字 → 第 5 步重建 → 第 9 步 drop 收尾」，并要求校验 `sqlite_master` 里 2 个触发器 + 2 个索引 | §7.1 |
| 建议 3 | v7 与新行的 `uuid` 来源不同，幂等键含义要写清 | **接受**。§7.1 补一段：迁移保留旧 uuid，新行统一按 `inbox_id+seq+kind` 派生 | §7.1 |
| 建议 4 | 测试缺 7 类场景（`sent` 被误收、say 误入 verify、outcome_unknown 联动、metrics 重复、迁移后全量回归、retract 对账、并发 append） | **接受**。测试清单 13 条 → **19 条**，逐条对应上面每个阻断项 | §8 |

## 我保留的判断（HY4 没提，但设计上继续这么做）

- `sent` 用**新状态名**而不是复用 `reply_verified`：语义上 say 没有被"读回确认"，
  复用会让 `verify_reply` 的统计口径混进不该校验的行。HY4 也主张显式加，不冲突。
- 迁移**不做向前兼容**：旧代码必须拒启。这是仓库纪律，HY4 也确认这条方向对。
---

# 附：第 1 轮深审（A 轮，更晚返回）

> 同一模型的另一轮评审，输入文件更多（`store.py`/`service.py`/`cli.py`/`AGENTS.md`/`design-b-loop.md`），
> 所以它发现了 B 轮没看见的 5 类问题。以下为要点与处置（原文已核，逐条验过行号）。

## A 轮结论

**不通过**：状态机、接口、迁移与 `store.py` 真实实现大面积不符，且未定义 dead 放行与迁移期间
in-flight send；照此开工必然静默丢状态或永久堵死。

## A 轮的 10 条阻断项 → v1.4 处置

| # | 它指出的问题 | 处置 |
|---|---|---|
| 1 | 状态 sending/sent/verified 不存在；claim 有 send/verify 双模式，另有 `mark_send_dispatched`/`outbox_verified`，删掉=幂等窗口/崩溃恢复/校验重试全废 | 接受：§3.1 用真实状态集；§5.2 补全 7 个方法 |
| 2 | `MAX(seq)+1` **漏 `WHERE inbox_id=?`** → seq 全局自增，违背「从 1 开始」 | 接受：§5.2 写死带 WHERE 的 SQL + 专门用例 |
| 3 | `NOT EXISTS` 只放行 sent/verified/dead/dropped；`needs_attention`/`outcome_unknown` 会让后续 seq **永久饿死** | 接受：§4 写死「放行状态全集」常量 + 两条用例 |
| 4 | `INSERT…SELECT` 把 v7 state 原样搬入，v8 状态机不认 → 取不到也不放行 | 部分接受：v1.4 明确 **v7→v8 状态恒等**（只新增 `sent`），因此不需要 CASE 映射；但补「迁移前断言无 in-flight」 |
| 5 | **未要求停 sender**：已 claim 后正在 `transport.reply()`，rename 后写新表 → 飞书已发、本库丢状态 → 重发 | 接受：§7 第 0/1 步「停服务 + 断言 `send_started/verifying` 为 0，否则中止」 |
| 6 | **WAL 下 `cp bridge.db` 不是一致备份**；「回滚只读旧表」与步骤 7-10 自相矛盾 | 接受：改 `VACUUM INTO`；删掉那句括号；实测 VACUUM INTO 可用 |
| 7 | 漏列必改点：`SCHEMA_VERSION`、旧 DDL 里的触发器列名、`UNFINISHED_OUTBOX_STATES`、5 处 `o.id=i.id` | 接受：§5.3 补齐，并处理「一 inbox 多行放大计数」 |
| 8 | **与上位文档冲突**：B-loop 的 v8 有 `chat_id NOT NULL`、主动消息（`original_id NULL`）、`events say --chat`、kind 仅 reply/say、retract 是改 state | 接受并按「一条事实一个答案」收敛：本文为唯一 DDL 来源；B-loop §5.1 改成链接 + 6 条不变量；v8 **收下** `chat_id` 与可空 `inbox_id`（主动消息），`retract` 仍用独立行（理由见 §3 取舍表） |
| 9 | **「出口唯一」今天不成立**：`notify` 直发 `send_dm`、`send_text` 直发回执；发送 API 在 `transport.py`，按字符串 gates 会禁错对象；仓库无 DELETE 撤回 API | 接受：§6 门禁改成「调用方白名单」（sender + notify 例外）；S1 新增 `Transport.delete_message`；`notify` 收编挂 S2 并写进 `CONTRACTS.md` |
| 10 | CLI 真实签名是 `--consumer/--event/--claim-token`；网关真实路径 `/v1/events/{id}/reply`，白名单写死 | 接受：§5.4 / §5.5 全部按真实签名与路由重写 |

## A 轮的 5 条建议 → 处置

| 建议 | 处置 |
|---|---|
| 并发 append / 两 sender 不重复 claim / say 幂等（`append_outbox` 无幂等入参） | 接受：`append_outbox(..., dedupe_uuid=)` + `ON CONFLICT(uuid) DO NOTHING`；两条并发用例 |
| `outbox_sent` 的 `attempt` 来源未定义 | 接受：§5.2 写明只来自 `claim_outbox` 返回的 `attempt_id`，`_live()` 校验 |
| retract 失败回一条会递归/seq 怎么算 | 接受：失败只 `fail_outbox`（不回消息），人工介入；重试路径见 §4 |
| 迁移要 `PRAGMA foreign_keys=ON`、禁 `executescript` | 接受：§7 第 3 步 + 落点说明 |
| `verify_reply` 只校验 reply、不提前置 completed | 已在 §3.1 固化（`outbox_verified` 才置 completed） |

## A 轮确认「设计稿正确」的 4 点（它自述已核对）

1. §7 先 DROP TRIGGER 再 RENAME 的顺序正确。
2. §4 反例判断正确（现有 claim 是 `ORDER BY next_due,created_at,id`，多行下确实乱序）。
3. §3「只锁内容不锁状态」正确（`BEFORE UPDATE OF` 列表里没有 `state`）。
4. §7 前置 `user_version=7` 正确，且 `store.py:64-68` 已有「更高版本拒启」分支。


---

# 附：第 3 轮（C 轮）——对 v1.4 的复评

## C 轮结论

**不通过**：状态名对得上，但四处会崩库——连锁改动表不穷尽、`Store.__init__` 的 DDL/版本门禁漏改、
`sent` 终态无实现且 inbox 副作用未收口、claim SQL 用了不存在的状态名。

## 处置（v1.5 已吸收，4 条阻断 + 4 条建议）

| # | C 轮意见 | 处置 |
|---|---|---|
| 1 | **只写外部迁移脚本，没改 `Store.__init__` 的 `executescript` DDL、`SCHEMA_VERSION`、版本链**（203-209 停在 7）→ 全新安装会建出 v7 表，而代码查 `row_id/inbox_id/seq`，必崩；且 64-68 只有上界，v7 库**静默穿过** | 接受：新增 §5.3「b3 全新安装路径」表（`SCHEMA_VERSION=8`、DDL 换 v8、`if version < 8` 推进、**补下界拒绝**），并加 `test_fresh_install_v8_schema` / `test_new_code_refuses_v7` |
| 2 | 连锁表又漏：`450`（prune）、`546/548`（**同一段里两个标识符**）、`503`（子查询喂给 inbox，必须 `inbox_id`）、`547/552/618`（inbox 级联）、`service.py:1032/1040-1041`；`tests/` fixture 与 `cli.py` 计数必连带失败 | 接受：补「b2」表 + 开工前两条全仓 grep 清单 + `test_no_outbox_dot_id_remains` 门禁。**并纠正了我 v1.4 的一处真错误**：495-505 的子查询不能改成 `row_id`（那行更新的是 inbox） |
| 3 | `sent` 无实现：`outbox_sent` 强制 `reply_id` 非空且只写 `verify_pending`；say 传 `None` 会 `ValueError`、传假值又被判 `mode=verify`。5 处把 outbox 级联到 inbox（503/547/552/602/618），§3 只 gate 了 602 | 接受：明确 `outbox_sent` 的校验**保留**、say/retract 只走 `outbox_marked`；所有 inbox 级联加 `kind='reply'`；加 `test_say_failure_does_not_touch_inbox` |
| 4 | claim SQL 用 `sending`（那是 reminders 的状态，1062）/ `verified`（真名 `reply_verified`）；候选集漏 `retry_wait/verify_pending`；完成集漏 `reply_verified/needs_attention`，与 §4.1 场景 3/6 矛盾 | 部分接受：v1.4 的 SQL 已是正确状态名与完整候选/完成集（这条基于 v1.3）；但仍加 `test_claim_matches_4_1` 钉住，并在 §3.1 写明 **`sending` 属于 `reminders`，别为"对齐"改 outbox** |
| 5 | `mid` 一码两用：`service.py:1032` 异常分支与 `1040-1041` `send_guard` 仍在用 `original_id or id` | 接受：§5.1 与 b2 表都补了这两处，加 `test_sender_row_id_vs_inbox_id`（由 `test_two_identifiers_not_swapped` 覆盖） |
| 建议 1 | `foreign_keys=ON` 下 v7 孤儿 `id` 会让 INSERT 整条失败、`original_id` 静默丢弃 → dry-run 要加两条断言 | 接受：§7 `--dry-run` 加「id 全在 inbox」「id=original_id」，加 `test_migration_detects_orphan` |
| 建议 2 | 「`dead` 由 cleanup 产生」不实（cleanup 只写 `dropped`）；`stats` 1426 硬编码未含 `sent`，会把只有 say 行的消息算进 `oldest_pending_at` | 接受：§3.1 改为「`dead` 当前无生产者，放行集里的 `dead` 是防御性放行」；1426 补 `sent`；加 `test_stats_sent_not_pending` |
| 建议 3 | 多行会让配额/latency/stats 重复计数 | 接受：加 `test_app_guard_counts_messages_not_rows` / `test_latency_one_sample_per_message` / `test_reply_conflict_ignores_say_rows` / `test_get_outbox_by_row_id` / `test_two_senders_claim_one_row` |
| 建议 4 | 类名是 `Store` 不是 `EventStore` | 接受：全文改名 |

## C 轮确认「正确」的 4 点

1. 10 个状态名在 `store.py` 全找得到，无编造无遗漏；reply 路径与 563/591/567/600 逐行吻合。
2. `sent` 不进 `UNFINISHED_OUTBOX_STATES` 正确。
3. §4 的「同 inbox 下 seq 最小」与场景 1/5/6 语义正确。
4. §7 第 3 步先删触发器/索引、逐条 `execute` 包 `BEGIN IMMEDIATE`、`user_version=8`、旧代码拒 v8 均与实测一致。
