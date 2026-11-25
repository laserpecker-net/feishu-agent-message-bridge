# PR-3 设计稿：活从哪来、谁来干（`work` 事件 + `dsh-worker`）

> 状态：**已实现**（2026-11-18，PR-3 收口；证据见 `docs/HISTORY.md` 与 CONTRACTS「活的派发与干活端」）。前置：PR-1（v10 迁移 + 角色路由，已收口）、PR-2（`Workbench`，已收口）。
> 本文只做「活得有人干起来」这一段；**把结果发回飞书是 PR-4**，别混。

---

## 1. 这一段解决什么

现在链路上有一个空洞：主人批了之后，`tasks.state='running'`，然后……没有人干活。

PR-3 补的是「**批准 → 生成一条活 → 干活端领走 → 真的跑起来**」。跑到什么程度算这一段完：活被 `dsh-worker`
领走、`Workbench.run()` 真的执行了、结果（成功或交接包）**落在账本上**。发群、发私聊留在 PR-4。

## 2. 活的产生：批准那一刻，事务内一起做

批准有三条入口（引用回复「做」、唯一待批、`执行` 命令），都收敛到 `Store.approve_task()`。**在同一个事务里**：

1. `tasks.state='running'`、`started_at=now`、`executor=NULL`（等干活端填）
2. 插一条 `work` 事件：
   ```json
   {"kind":"work","dedupe_key":"work:<task_id>",
    "payload":{"task_id":"…","chat_id":"…","origin_mid":"…",
               "requirement":"群里原话","hints":["主人批卡时补的话"],
               "worktree_branch":"work/<task_id>"}}
   ```
3. **幂等**：`dedupe_key` 唯一，重复批准/重投只会有这一条（`INSERT OR IGNORE`，插没插进去要如实返回）

不进 `work` 事件的三种情况（要有明确理由，不是"顺手不写"）：任务不是 `open`（已批过）、批准人不是主人、
来源是 app 身份。这三条各有一个用例。

**为什么 payload 里要带 `requirement` 和 `hints`**：干活端不应该再去翻群消息——它拿到的就是一个完整任务包。
回群的话术由 PR-4 决定，这里只保证数据齐。

## 3. 谁来干：`dsh-worker.py`

新进程，和回话端 `dsh-consumer.py` 并列：

| 项 | 值 |
|---|---|
| 消费者名 | `dsh-worker`，`role='worker'` |
| launchd 单元 | `io.feishu-agent.dsh-worker`（`install.py` 负责写、`--verify` 负责重载） |
| 领什么 | 只领 `kind='work'`（PR-1 的角色路由已经强制，抢错会报错） |
| 并发上限 | `worker.max_concurrency`，**默认 1**：本机同一时刻只跑一件活（回到"一台机器一个人"的常识） |
| 下班 | 主人发 `下班` → 不领新活；正在跑的那件发 `stop` → 交接包 + `failed` |

**启动自检（不是空转）**：`Workbench.prepare()` 不过就**直接失败退出**并打印下一步（例如"dsh 起不来，先修
`workbench.cli`"），不进入"看起来在跑、其实领不了"的状态。

## 4. 在哪儿干：每个活一个 worktree

2026-11-19 更正（主人拍板）：**干活端不假设机器上有 git。** 每个活一个独立目录
`<bridge home>/work/<task_id>/space/` 就是 agent 的 `cwd`，产物写在这里；**不建工作树、不切分支、不提交**。
理由：除了我们这台开发机，同事机上根本没有 git——"开工作树 + 提交"只对开发机成立，不是通用形态。

- 目录建不出来 → `blocked`（`kind='workdir'`）+ 交接包，不许硬干
- 产物 = `space/` 里的文件清单（相对路径 + 字节数），进 `tasks.artifacts`/note/群回执；
  上限 `ARTIFACT_LIMIT=200` 条，超限只截清单**不判失败**
- `worker` 配置只有 `consumer_name` / `max_concurrency`；**没有** `repo` / 工作树配置

## 4.1 早期的 git 工作树方案（已废弃，留档说明为什么变）

当时的设计是 `git worktree add <root>/<task_id> -b work/<task_id> HEAD`，跑完由干活端提交。
它在**开发机**上能跑通，但引入两处平台假设：① 必须有 git；② 沙箱写不进工作树的 `.git`
（提交只能由 worker 进程做）。既然同事机没有 git，整套前提不成立，于是收敛成"一个活一个目录"。

## 5. 账本怎么记

| 时刻 | 账本动作 |
|---|---|
| 批准 | `tasks.state='running'`、`started_at`、`work` 事件落库 |
| 领走 | 事件 `state='leased'`（PR-1 的 claim）、`tasks.executor='dsh-web@<机器名>'` |
| 跑起来 | 运行目录 `<bridge home>/work/<task_id>/`；进度边跑边落盘（PR-2 已实现） |
| 成功 | 事件 `ack`、`tasks.state='done'`、`artifacts`、note 写耗时与 worktree 路径（**发群留给 PR-4**） |
| 失败/交接 | 事件 `ack`、`tasks.state='failed'` 或 `'blocked'`、交接包落 `work/<task_id>/handoff.*`，并把 `failure` 写进 note |

**事件失败 vs 任务失败要分清**：干线本身出错（比如账本写不进去）→ `fail_event` 让它按既有重试语义走；
**活的结论**（超时/锁/凭证）→ 事件 `ack` 掉 + 任务落 `failed/blocked`——因为"事件没人领"和"活干不成"是两件事。

## 6. 失败语义（PR-3 只落账，PR-4 才说话）

| 现场 | 事件 | 任务 | 交接包 |
|---|---|---|---|
| `Workbench.prepare()` 失败 | ack | `blocked` | 有（`cli_unavailable`/`credential_missing`） |
| worktree 创建失败 | ack | `blocked` | 有 |
| 运行超时 / 机器睡过去 | ack | `failed` | 有（PR-2 已保证） |
| 会话锁冲突 | ack | `blocked` | 有 |
| 正常完成 | ack | `done` | 不需要 |

## 7. 测试（约 12 条）

1. 批准 → 恰好一条 `work` 事件，`dedupe_key='work:<task_id>'`，payload 六个字段齐
2. 重复批准/重投 → **仍然只有一条**（幂等）
3. 未批准的任务 → 不产生 `work` 事件
4. 非主人批准 / app 身份 → 不产生（三条边界各一）
5. `replier` 领 `work` → 报错（PR-1 已有，这里防回归）
6. `dsh-worker` 领到后 `tasks.executor` 有值
7. worktree 创建成功：目录存在、分支名对、`HEAD` 等于基线
8. worktree 已存在/主树有 rebase → `blocked` + 交接包
9. `max_concurrency=1`：同时两条 work，第二条留 pending（不并发领）
10. `下班` 后 worker 不领新活
11. `stop` → 交接包落账、任务 `failed`
12. launchd 单元与 `install.py` 快照（单元名、参数、role=worker）

## 8. 风险

| 风险 | 处置 |
|---|---|
| 工作树越攒越多 | 跑完保留是有意的；保留天数与清理归 S5，编排里已记 |
| 重复开跑 | `dedupe_key` 唯一 + `tasks.state` 判断，双保险 |
| 干活端和主人在同一个 `~/.dsh` 抢会话 | 会话 id 由 `task_id` 派生；锁冲突按设计落 `blocked` |
| 产物目录里旧文件混进来 | 每个活一个新目录；同一件活重派时上一次现场挪进 `attempts/<时间戳>/`，不覆盖 |

## 9. 估时

**0.5–1 天**。风险集中在"launchd 单元 + install.py 接线"和"并发/下班"这两处，其余是账本逻辑。

## 10. 不做

- 不发群、不发私聊、不写 outbox（PR-4）
- 不做浏览器（S3b）
- 不做 Windows（S3b）
- 不做「同一件活跑失败自动重试」（重启策略是主人的决定，不是默认行为）

---

## 11. 后续补充（2026-11-19，A 方案）：主人自己下的写活

原设计只写了"批准才派活"，没写"主人自己下的令"怎么办。生产上真出过一次：回话端直接替他
改了仓库并提交，没有工作树、没有账本。补的规则是：

- 判据：主人本人在群里下令的**写活** → 不发卡，发 `direct_work`（键与 `task_card` 相同，
  但 `touches`/`done_when` 缺一不派）；
- 落账：`tasks.approval_kind='owner_direct'`，`approval_mid` = 那条消息 id，`approved_at`/`started_at` 同刻；
- 执行：仍然走 `work` 事件 + `dsh-worker` + 独立工作树，群里先回一句「收到，派给干活端了」；
- 边界：别人 / app 身份永远拿不到这一档；同一条消息重投只产生一个任务。
