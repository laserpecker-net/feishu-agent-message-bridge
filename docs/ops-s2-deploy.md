# S2（tasks v9 与任务卡审批）部署与回滚

> 适用版本：含 `tools/migrate-tasks-v9.py` 的 S2 代码（PR-1 及之后）。本文是**执行清单**，不是设计说明；
> 现在该怎么做见 [CONTRACTS.md](../CONTRACTS.md)（任务卡与审批、出站唯一出口），
> 为什么这么定与实施偏差见 [HISTORY.md](HISTORY.md)（2026-11-17 夜）。
> 全程不改配置、不手工改表；每一步都有可复跑判据。

---

## 0. 执行前先确认这四件事

| # | 事实 | 怎么看 |
|---|---|---|
| 1 | 生产库版本 | `sqlite3 "$DB" 'PRAGMA user_version'` → 应为 **8**（S1 已上线） |
| 2 | `tasks` 现在几行、几列 | `sqlite3 "$DB" 'SELECT COUNT(*) FROM tasks'`、`sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('tasks')"` → 列数应为 **10** |
| 3 | 线上跑的是哪份代码 | `feishu-bridge status` 里的 `running_release`；它指向 `…/current` 的副本，**不是本仓库** |
| 4 | 服务在跑 | `feishu-bridge status` → `mode: running` |

### 0.1 为什么是「两步升级」，不能跳

库的版本链是 **v7 →(S1 工具)→ v8 →(S2 工具)→ v9**。代码对**两个**旧版本都做下界拒绝，
因为缺的列不一样、报错指向的工具也不一样：

| 库版本 | 新代码启动时的真实报错（中文，逐字） |
|---|---|
| v8 | `状态库还是 v8，代码要 v9：先跑 python3 tools/migrate-tasks-v9.py --db <库文件>` |
| v7 | `状态库还是 v7，代码要 v9：先跑 python3 tools/migrate-outbox-v8.py --db <库文件>（迁到 v8 之后再跑 python3 tools/migrate-tasks-v9.py）` |

跳步的后果不是"少一列"，而是**启动失败**——这是设计选择：宁可起不来，也不带着缺列跑，
把「没迁移」伪装成「代码有 bug」。

### 0.2 关键约束：先迁库、再换 release

- **新代码 + v8 库** = 拒绝启动（上面那条中文提示）。
- **旧代码 + v9 库** = 拒绝启动，英文：`RuntimeError: Unsupported state schema version 9`。

所以顺序只能是「停服 → 迁库 → 换 release」。反了就是一次没必要的停机事故。

---

## 1. 部署（停机约 2 分钟）

```bash
H="$HOME/Library/Application Support/FeishuPersonalAgent"
DB="$H/state.sqlite3"
```

### 1.1 停服务

```bash
feishu-bridge stop
feishu-bridge status      # 期望服务已停；running_release 仍是旧 release
```

### 1.2 查在途出站行

```bash
sqlite3 "$DB" "SELECT row_id,kind,state FROM outbox
               WHERE state IN ('queued','send_started','verifying','verify_pending','retry_wait')"
```

- **0 行** → 进 1.3。
- **>0 行** → 先起回旧服务让它自己收尾，再停服重查；**不要**手工 `UPDATE outbox`。
  S2 不改出站状态机，这些行与本次迁移无关，但停机期间它们不会自己前进。

### 1.3 预演（不备份、不写库）

```bash
python3 tools/migrate-tasks-v9.py --db "$DB" --dry-run
```

期望：`dry-run 通过：{'rows': N, 'columns': [...10 列...]}`，退出码 0（只报行数与现有列，不写库）。
库不是 v8（比如已是 v9）会直接拒绝——那是"别重复迁移"，不是错误。

### 1.4 实迁（自动备份，备份 chmod 0600）

```bash
python3 tools/migrate-tasks-v9.py --db "$DB"
```

打印三行 + 一段自检 JSON：

```
备份：…/state.sqlite3.v8-<时间戳>
迁移完成：{'user_version': 8, 'rows': N, …} -> {'rows': N, 'added': [...8 列...], 'columns': [...18 列...]}
{ "user_version": 9, "rows": N, "column_count": 18, "has_all_v9_columns": true,
  "foreign_key_check": [], "integrity_check": "ok" }
自检通过
```

**把这一整屏贴进回执**——回滚要用备份文件名。备份是用 `VACUUM INTO` 做的自洽单文件，
**不需要**配套 `-wal`（这点在回滚时很重要）。

### 1.5 切 release（会自动激活重启）

```bash
cd ~/Documents/feishu-personal-agent
python3 install.py --verify
```

`install.py` 的顺序：构建并校验新 release → 原子切 `current`（提交点）→ 写 config/服务定义 →
**激活重启** → `--verify` 再跑一次 `doctor --assert`。退出码 0 才算装完；3 表示「代码已提交但没跑起来」。

### 1.6 验收（四条判据，逐条贴输出）

```bash
sqlite3 "$DB" 'PRAGMA user_version'                          # 期望 9
sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('tasks')"  # 期望 18
sqlite3 "$DB" 'SELECT COUNT(*) FROM tasks'                   # 与 0.2 记录的、迁移前一致
sqlite3 "$DB" 'PRAGMA integrity_check'                       # ok
feishu-bridge status                                          # running_release = 新 release；running
feishu-bridge doctor --assert; echo "rc=$?"                   # 期望 rc=0
```

---

## 2. 回滚（停机约 1 分钟）

### 2.1 停服务

```bash
feishu-bridge stop
```

### 2.2 用备份覆盖主库

```bash
cp "$H/state.sqlite3.v8-<时间戳>" "$H/state.sqlite3"
rm -f "$H/state.sqlite3-wal" "$H/state.sqlite3-shm"      # 必须删，原因见下
```

**为什么必须删 `-wal` / `-shm`**：WAL 里存的是「相对某个数据库文件版本」的帧与页号。把库换成备份后，
若旧 WAL 还躺在旁边，SQLite 打开时会尝试用这些帧去「恢复」它——轻则把新库的页盖回旧内容，
重则 `database disk image is malformed`。最保险的规则：**换库文件 = 连它的 `-wal`/`-shm` 一起换**。
（备份是 `VACUUM INTO` 出来的单文件，本身不带 WAL。）

### 2.3 切回旧 release 并启动

```bash
feishu-bridge rollback --list          # 找到迁移前那个 16 位 hex release id
feishu-bridge rollback --to <旧 id>
feishu-bridge start
```

### 2.4 验收

```bash
sqlite3 "$H/state.sqlite3" 'PRAGMA user_version'   # 期望 8
sqlite3 "$H/state.sqlite3" "SELECT COUNT(*) FROM pragma_table_info('tasks')"  # 期望 10
feishu-bridge status
feishu-bridge doctor --assert; echo "rc=$?"
```

**迁移过的库不能给旧代码用**：v9 库给旧 release 打开会得到
`Unsupported state schema version 9`。所以回滚必须是「库 + release 一起退」，
不存在"只退代码"这种半回滚。

---

## 3. 两个「半态」及其后果（都要能在 30 秒内认出来）

| 半态 | 现象 | 处理 |
|---|---|---|
| **库已 v9、`current` 没换** | 启动失败：`Unsupported state schema version 9`（英文，旧代码的上界拒绝）；不是 bug | 二选一：装新 release（§1.5），或按 §2 把库退回 v8 |
| **`current` 换了、库没迁** | 启动失败：中文「状态库还是 v8，代码要 v9：先跑 python3 tools/migrate-tasks-v9.py…」 | 按 §1.3 / §1.4 迁库，再 `feishu-bridge restart` |

两个半态都**不会**「跑起来但写错数据」——宁可启动失败，也不降级运行。

---

## 4. 真机验收（3 分钟，需要主人在飞书里配合）

前提：这个时间窗里**只有一张**待批卡片（否则必须引用对号，见 §4.3 反例）。

1. 主人在群里 @ 机器人一件需要他点头才做的事 → 机器人**私聊**发来一张五段任务卡
   （【活】【为什么是我】【打算怎么做】【会碰到什么】【怎么算做完】），群里回执写明「卡已发你私聊，你批了我才动」。
2. 主人**引用那张卡片**，回一句 `做，但只做①`。
3. 断言（可粘贴）：

```bash
DB="$HOME/Library/Application Support/FeishuPersonalAgent/state.sqlite3"
# ① 账本：running + 审计三件套有值
sqlite3 -header -column "$DB" "SELECT id,state,
       approval_kind AS 靠哪一档, substr(approval_mid,1,18) AS 批准消息,
       datetime(approved_at,'unixepoch','localtime') AS 批准时间
       FROM tasks ORDER BY created_at DESC LIMIT 1"
# ② hints：主人的原话整段在（JSON 里能搜到「只做」）
sqlite3 "$DB" "SELECT hints FROM tasks ORDER BY created_at DESC LIMIT 1"
# ③ 群里那句回执（say 行）
sqlite3 -header -column "$DB" "SELECT row_id,kind,state,substr(body,1,40) AS 内容
       FROM outbox WHERE kind='say' ORDER BY row_id DESC LIMIT 3"
```

期望：`state=running`、`approval_kind=quoted`、`approval_mid` 非空、`approved_at` 有值、
`hints` 里能读出「做，但只做①」原文、群里出现一句回执。

> S2 里**没有干活端**，所以批准后任务停在 `running` 是设计内的（干活端是 S3a），不是卡住。

### 4.1 反例判据（收紧后的判据必须成立）

主人引用同一张卡，只回一句「我看看」或「把第 2 步改成先备份」：

- 期望：私聊收到**追问**（要我做就回「做」，不做就回「不做」）；
- 期望：`tasks.state` 仍然 `open`，`hints` 长度没变（含糊的话不落账）。

这一条是 2026-11-17 主人拍板收紧的判据（见 `docs/HISTORY.md`）：引用只证明"在答这张卡"，
不证明"同意做"。

---

## 5. 失败处置（一条命令级，别靠猜）

| 现象 | 先做什么 | 判据 |
|---|---|---|
| 迁移中途出错 | 事务已回滚，库仍是 v8；`python3 tools/migrate-tasks-v9.py --db "$DB" --verify` | `user_version=8`、`column_count=10` |
| 想验证回滚本身 | `python3 tools/migrate-tasks-v9.py --db <临时副本> --inject-fault`（**只对副本**） | 退出码 1、库仍是 v8 |
| 迁移后服务起不来 | `feishu-bridge status` + 日志里 `RuntimeError` | 中文=库没迁（§1.4）；英文 `Unsupported … 9`=release 没换（§1.5） |
| 验收 ① 不通过（任务没 running） | 先看 `feishu-bridge status` 的 `pending_approval`，再看 `SELECT state,approval_mid FROM tasks` | 卡片还在 `open` + 没有 `approval_mid` = 批准没被判成（看是不是含糊答复，§4.3） |
| 验收 ③ 不通过（群里没回执） | `SELECT row_id,state,error FROM outbox ORDER BY row_id DESC LIMIT 5` | `state=queued` 且服务在跑 = sender 没起；`needs_attention` = 看 `error` |

---

## 6. 判据汇总（可复跑，谁执行谁贴输出）

```bash
H="$HOME/Library/Application Support/FeishuPersonalAgent"; DB="$H/state.sqlite3"
sqlite3 "$DB" 'PRAGMA user_version'                                  # 9
sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('tasks')"      # 18
sqlite3 "$DB" 'SELECT COUNT(*) FROM tasks'                           # 与迁移前一致
sqlite3 "$DB" 'PRAGMA foreign_key_check'                             # 空
sqlite3 "$DB" 'PRAGMA integrity_check'                               # ok
ls -l "$DB".v8-*                                                     # 备份 0600、非空
feishu-bridge status                                                 # running + 新 release
feishu-bridge doctor --assert; echo rc=$?                            # rc=0
python3 -m pytest tests/test_tasks_v9_migration.py -q                # 迁移/门禁用例绿
```

---

## 7. 出事时的三个「别做」

1. **别**把备份 `cp` 回去却留着 `-wal`/`-shm`（§2.2 的原因）。这是最容易造成二次损坏的一步。
2. **别**手工 `ALTER TABLE` 或 `UPDATE tasks` 去"补"一列/改一个状态；要走就重跑迁移或按 §2 回滚。
3. **别**为了绕过版本门禁去改 `SCHEMA_VERSION`——那会让「库和代码不一致」变成静默运行，
   下一次排查成本高一整个数量级。
