# S3a 编排：干活端（mac，`dsh-web` 直连）

> 这是**执行清单**，不是设计说明。设计见 [design-b-loop.md](design-b-loop.md)（S3a 起那半份）。
> 目标一句话：**群里有人要一件要动手的活 → 主人批准 → 真有人干 → 结果带着引用回到群里**。
> 现状（2026-11-18）：S1/S2 已上线，批准之后任务停在 `running`，**还没有人干活**。本切片补的就是这一步。

---

## 0. 完成的判据（七条，缺一不算完）

| # | 判据 | 怎么看 |
|---|---|---|
| 1 | 一件**真活**端到端跑通 | 群里发需求 → 私聊批准 → 群里出现「结果」的**带引用追答**，且 `tasks.state='done'` |
| 2 | 结果**引用原来那条消息** | `outbox.kind='say'` 且 `parent_id` 指向原消息；不是新开一条 |
| 3 | **失败也要回群** | 人为制造失败（超时/锁冲突/凭证缺失）→ 群里有一句人话解释 + `tasks.state='failed'/'blocked'` |
| 4 | 锁冲突 → `blocked` + 问一句 | 会话被主人占着时，落 `blocked` 并开 `open_question`，**不重试硬闯** |
| 5 | 角色路由生效 | `message/attention` 只能 `role=replier` 领；`work` 只能 `role=worker` 领；抢错要报错 |
| 6 | 每次干活的账都能回答「谁批的、谁干的、多久」 | `tasks.approval_*` + `executor` + 耗时写进 note |
| 7 | 判据可复跑 | 全量测试无管道跑（`> 文件; rc=$?`）+ 20 轮无 `rc=139` + `doctor --assert` rc=0 |

---

## 1. 要动的点（21 处，按文件）

### 1.1 状态库（`bridge/store.py` + `tools/migrate-work-v10.py`）

1. `consumers` 加 `role TEXT NOT NULL DEFAULT 'replier'`
2. `EVENT_KINDS` 加 `'work'`
3. 领取按角色过滤：`message/attention` → `replier`；`work` → `worker`；**别的角色领到就报错**（不是静默跳过）
4. `events.claim_event` / `gateway` / CLI 的 `events claim` 都带角色过滤（一处实现，三处调用）
5. 迁移工具：`--db/--dry-run/--verify`，`VACUUM INTO` 备份 + `chmod 600`，断言行数不变
6. 版本门禁：代码要 v10；v9 库拒绝启动并指向本工具；v10 库被旧代码打开同样拒绝

### 1.2 干活端消费者（新 `dsh-worker.py` + launchd 单元 + `install.py`）

7. 新进程 `dsh-worker.py`：注册 `role='worker'`，只领 `work` 事件
8. launchd 单元 `io.feishu-agent.dsh-worker`；`install.py` 负责写/重载
9. 与 `dsh-consumer.py` 共用一套 claim/ack/fail 纪律（同一份代码路径，不复制）

### 1.3 工作台适配器（新 `bridge/workbench.py`）

10. `Workbench.prepare()`：cli 可执行、`--version` 能答、home 有凭证、workspace 可写、**与工作台同一个 home**
11. `Workbench.run(prompt, session_id)`：`dsh headless --patch <overlay> --json -`，流式读 NDJSON
12. 凭证 overlay：0600 临时文件，跑完立刻删（复用 `dsh-consumer.py` 的现成做法，不另写一套）
13. **「没有 `final` = 失败」**：机器睡过去时进程消失、退出码交不出来
14. 超时/取消要收进程树（`start_new_session` + `killpg`）
15. 锁冲突识别：stderr/stdout 里 `already owned by an active write handle` → `blocked`

### 1.4 活的产生与结果的回写（`bridge/service.py`、`bridge/approval.py` 调用面）

16. 批准那一刻（引用回复「做」/唯一待批/`执行`）：`tasks.state='running'` + **发一条 `work` 事件**（payload：`task_id/chat_id/origin_mid/body/hints/acceptance/reply_with`）
17. 任务包模板（prompt 骨架）：原话 + 主人的补充（`hints`）+ 验收标准 +「结果要能直接贴回群里」
18. 干活端拿到结果后：`events say` 带引用回群（`reply_with` 指定挂在哪条）+ `tasks.state='done'` + note 写耗时与产物路径
19. 三种失败各回一句人话：超时 / 锁冲突（→ `blocked` + 追问）/ 凭证缺失
20. `executor` 写清是哪台机器、哪个会话 id；重放同一 `work` 事件要幂等（同一 task 不重复开跑）
21. 任务卡回执补一句「批准了，干活那端接着干」——**S3a 之后这句才为真**

---

## 2. 交付顺序（四个 PR + 一个收尾）

依赖是硬的：**迁移先行**（1.1 不落地，后面所有东西都没有地方挂），**收尾最后**（真机验收才能证明全链）。

| PR | 内容 | 判据（本地可复跑） | 估时 |
|---|---|---|---|
| **PR-1 迁移与角色路由** | 1.1 全部 + CLI/gateway 的角色过滤 | 迁移：fresh vs migrated `PRAGMA table_info(consumers)` 相等；v9 库拒绝启动文案逐字；`role` 路由三类用例（replier 领 message ✅ / 领 work ❌ / worker 领 work ✅） | 0.5 天 |
| **PR-2 工作台适配器** | 1.3 全部 | 假 cli（用脚本模拟 NDJSON）跑通：正常出 `final`、无 `final` 超时、锁冲突文案识别、进程树收干净、overlay 用完即删（0600 断言） | 1 天 |
| **PR-3 活产生 + 干活端消费者** | 1.2 + 1.4 的 16/17/20 | 批准 → `work` 事件落库且 `role=worker` 才领得到；重复批准不产生第二条 work；worker 起得来、bot 身份领不到 | 0.5 天 |
| **PR-4 结果回写与失败语义** | 1.4 的 18/19/21 | 四种结局各一条用例（done/超时/锁/凭证）；`say` 行的 `parent_id` 指向原消息；`tasks.state` 与 note 对得上 | 0.5 天 |
| **收尾 真机验收 + 文档** | 真机跑一件活；`ops-s3a-deploy.md`；HISTORY 一篇；CONTRACTS 更新 | §0 七条逐条贴输出 | 0.5 天 |

**合计 3–3.5 人日**（设计稿估 1.5–2 天；S2 的教训是这类切片实际会翻倍，这里把验收与失败语义算足）。

---

## 3. 每个 PR 的固定门禁（一个都不许省）

```bash
# 1) 全量测试，不许用管道吃掉退出码
/usr/bin/python3 -m pytest -q > /tmp/t.log 2>&1; echo rc=$?

# 2) 脱敏 + 出站唯一
/usr/bin/python3 -m pytest tests/test_repo_hygiene.py tests/test_single_exit.py -q > /tmp/h.log 2>&1; echo rc=$?

# 3) 连接模型的回归（这次 SIGSEGV 之后新增）
grep -rn "check_same_thread=False" bridge/ tools/ tests/ ; # 必须零命中
```

**PR 级还要加**：连续 20 轮全量 0 次 `rc=139`（只在 PR-1、PR-4、收尾各跑一次；中间两个 PR 跑 3 轮）。

---

## 4. 真机验收脚本（3 分钟，需要主人在飞书里配合）

1. 群里发一条**要动手**的需求（例如「把这三个 csv 汇总成一张表，发回来」）
2. 机器人私聊发卡 → 主人引用回「做」或发 `执行`
3. 断言（可粘贴）：

```bash
DB="$HOME/Library/Application Support/FeishuPersonalAgent/state.sqlite3"
# ① 活已经产生并且被领走
sqlite3 -header -column "$DB" "SELECT id,kind,state,consumer,datetime(created_at,'unixepoch','localtime')
       FROM events WHERE kind='work' ORDER BY id DESC LIMIT 3"
# ② 任务状态与执行者
sqlite3 -header -column "$DB" "SELECT id,state,executor,approval_kind,
       datetime(approved_at,'unixepoch','localtime') 批准, datetime(started_at,'unixepoch','localtime') 开工,
       substr(note,1,60) 备注 FROM tasks ORDER BY created_at DESC LIMIT 1"
# ③ 群里那条带引用的追答
sqlite3 -header -column "$DB" "SELECT row_id,kind,state,parent_id,substr(body,1,50)
       FROM outbox WHERE kind='say' ORDER BY row_id DESC LIMIT 3"
```

4. 反例（各一次，都要有人话回群）：
   - 主人**开着同一个会话**时再派一件 → `blocked` + 追问
   - 把 CLI 路径临时指到不存在 → `failed` + 「我这边工具起不来」

---

## 5. 回滚（v10 只有一次单向门）

**顺序只能是**：停服 → `tools/migrate-work-v10.py --db <库>` → `install.py --verify` → 验收。

- 迁移中断：事务回滚，库仍是 v9；用 `--verify` 确认 `user_version=9`
- 要退：恢复 `state.sqlite3.v9-*` 备份 **并删 `-wal`/`-shm`**（WAL 帧相对文件版本），再
  `feishu-bridge rollback --to <旧 release>`——**库和 release 必须同进退**
- 干活端单独退：`launchctl bootout` 掉 `dsh-worker` 单元即可，不影响回话端

---

## 6. 风险表

| 风险 | 触发条件 | 处置 |
|---|---|---|
| token 烧穿 | 一件活跑了半小时还没完 | `timeout_seconds` 默认 1800，超时先杀进程树再回群；note 里记耗时 |
| 会话锁冲突 | 主人正开着同一个会话 | 识别固定文案 → `blocked` + 追问；**不重试** |
| 与主人 GUI 抢 home | 干活端和 GUI 同一个 `~/.dsh` | 设计上**允许**同 home、不同会话；会话 id 由任务 id 派生，避免撞名 |
| 孤儿 dsh 进程 | 超时/取消 | `start_new_session` + `killpg`；验收里用 `pgrep` 确认零残留 |
| 重复开跑 | 同一 `work` 事件被重投 | 以 `task_id` 作幂等键：`state='running'` 且有 `started_at` 就不再开 |
| 结果太长贴不回群 | 干活输出几千行 | 回群只发结论 + 产物路径；完整产物落 `artifacts/`，群里给路径 |

---

## 7. 已定的事（原来是要拍板的三件）

1. **workspace = 本仓库**（`~/Documents/feishu-personal-agent/`；主人 2026-11-18 定：
   活就是干在这个项目上，不是别的目录；原来提议的 `~/Documents/ai-market-team` 只是报告产物目录，**不是 git 工作树**，作废）
2. **每个活一个独立目录**（2026-11-19 主人纠正后定）：`<bridge home>/work/<task_id>/space/` 是 agent 的
   `cwd`，产物写在这里；**不假设机器上有 git**（同事机没有 git）——不建工作树、不切分支、不提交。
   回执给的是「产物：`space/x.csv`（12.3 KB）」，不是提交号
3. **S3a 不碰浏览器**：浏览器独占性留到 S3b 跟锁检测一起做；S3a 只用命令行能力

## 8. 交给 S3b 的前置

- 版本先对齐：两端 `dsh` 同一个版本（现在 mac 是 `0.2.2-rc.4`，开发机 `0.2.2-rc.3`）
- `Workbench` 的 `launch` 字段已经预留 `cmd-interop`，S3b 只加 `platform_paths.py` 与进程树收尾，不动状态机
- 失败语义的四种结局在 S3a 已经钉住，S3b 只补平台差异（`taskkill /T`、WSL 路径）
