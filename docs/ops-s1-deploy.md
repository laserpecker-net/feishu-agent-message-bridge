# S1（outbox v8）部署与回滚

> 适用版本：`a15ee44` 及之后的 S1 代码。本文是**执行清单**，不是设计说明；
> 设计见 [design-s1-outbox-v8.md](design-s1-outbox-v8.md)，计划见 [plan-s1-outbox-v8.md](plan-s1-outbox-v8.md)。
> 全程不改配置、不手工改表；每一步都有可复跑判据。

---

## 0. 执行前先确认这三件事

| # | 事实 | 怎么看 |
|---|---|---|
| 1 | 生产库在哪、什么版本 | `sqlite3 "$HOME/Library/Application Support/FeishuPersonalAgent/state.sqlite3" 'PRAGMA user_version'` → 应为 **7** |
| 2 | 行数与状态分布（迁移前后必须一致） | `sqlite3 <库> 'SELECT state,COUNT(*) FROM outbox GROUP BY state'` |
| 3 | 线上跑的是哪份代码 | `feishu-bridge status` 里的 `running_release`；它是 `~/Library/Application Support/FeishuPersonalAgent/current` 的副本，**不是本仓库**——所以本仓库改完不影响正在跑的进程 |

> 关键约束：**新代码遇到 v7 库会拒绝启动**（中文提示去跑迁移）。所以「先迁库、再换 release」，
> 顺序反了就是一次没必要的停机事故。

---

## 1. 部署（停机约 2 分钟）

```bash
H="$HOME/Library/Application Support/FeishuPersonalAgent"
DB="$H/state.sqlite3"
```

### 1.1 停服务

```bash
feishu-bridge stop
feishu-bridge status      # 期望 running_release 仍指向旧 release，服务已停
```

为什么先停：sender 线程停下后没人再 claim，才不会出现「停服瞬间发出一半、库里状态没落地」。

### 1.2 查在途行（唯一可能导致迁移中止的东西）

```bash
sqlite3 "$DB" "SELECT row_id,kind,state FROM outbox WHERE state IN ('send_started','verifying')"
```

- **0 行** → 直接进 1.3。
- **>0 行** → 二选一（不要自己改表）：
  1. 起回旧服务让它自己收尾（租约过期后由 `claim_outbox` 回收），再重新停服、重查；
  2. 停服后确认这些行确实需要人工核对，实迁时加 `--park-inflight`：它们会被停成
     `needs_attention`，`error` 写「迁移时仍在发送中；未确认是否已发出，需人工核对」。
     **不会**被改成 `queued`——那等于重发一条可能已经发出去的消息。

### 1.3 预演（不写库、不备份）

```bash
python3 tools/migrate-outbox-v8.py --db "$DB" --dry-run
```

期望输出形如 `dry-run 通过：{'rows': N, 'states': {...}}`；若有在途行，会逐条点名并提示加
`--park-inflight`。

### 1.4 实迁（自动备份，备份 chmod 0600）

```bash
python3 tools/migrate-outbox-v8.py --db "$DB"            # 有在途行时：加 --park-inflight
```

打印：`备份：<库>.v7-<时间戳>`、`迁移完成：{...} -> {...}`、自检 JSON、`自检通过`。
**把这一整屏贴进回执**——回滚要用备份文件名。

### 1.5 切 release（会自动激活重启）

```bash
cd ~/Documents/feishu-personal-agent
python3 install.py --verify
```

`install.py` 的顺序是：构建并校验新 release → 原子切 `current`（提交点）→ 写 config/服务定义 →
**激活重启** → `--verify` 再跑一次 `doctor --assert`。退出码 0 才算装完；
3 表示「代码已提交但没跑起来」。

### 1.6 验收（三条判据，逐条贴输出）

```bash
sqlite3 "$DB" 'PRAGMA user_version'          # 期望 8
feishu-bridge status                          # running_release = 新 release；running
feishu-bridge doctor --assert; echo "rc=$?"   # 期望 rc=0
```

---

## 2. 回滚（停机约 1 分钟）

### 2.1 停服务

```bash
feishu-bridge stop
```

### 2.2 用备份覆盖主库

```bash
cp "$H/state.sqlite3.v7-<时间戳>" "$H/state.sqlite3"
rm -f "$H/state.sqlite3-wal" "$H/state.sqlite3-shm"      # 见下，必须删
```

**为什么必须删 `-wal` 和 `-shm`**：WAL 里存的是「相对某个数据库文件版本」的帧与页号。
把库换成备份后，若旧 WAL 还躺在旁边，SQLite 打开时会尝试用这些帧去「恢复」它——
轻则把新库的页盖回旧内容，重则 `database disk image is malformed`。
最保险的一条规则：**换库文件 = 连它的 `-wal`/`-shm` 一起换**（备份是用 `VACUUM INTO` 做的，
是自洽单文件，不需要配套 WAL）。

### 2.3 切回旧 release 并启动

```bash
feishu-bridge rollback --list          # 找到迁移前那个 16 位 hex release id
feishu-bridge rollback --to <旧 id>
feishu-bridge start
```

### 2.4 验收

```bash
sqlite3 "$H/state.sqlite3" 'PRAGMA user_version'   # 期望 7
feishu-bridge status
feishu-bridge doctor --assert; echo "rc=$?"
```

---

## 3. 两个「半态」及其后果（都要能在 30 秒内认出来）

| 半态 | 现象 | 处理 |
|---|---|---|
| **库已 v8、`current` 没换** | 启动失败，日志里是 `RuntimeError: Unsupported state schema version 8`（英文）；这是设计里的「旧代码打不开新库」，不是 bug | 二选一：装新 release（1.5），或按 §2 回滚库到 v7 |
| **`current` 换了、库没迁** | 启动失败，中文报错「状态库还是 v7，代码要 v8：先跑 python3 tools/migrate-outbox-v8.py --db <库文件>」 | 按 1.3 / 1.4 迁库，再 `feishu-bridge restart` |

两个半态都不会「跑起来但写错数据」——这一点是设计选择（宁可启动失败，也不降级运行）。

---

## 4. 判据汇总（可复跑，谁执行谁贴输出）

```bash
H="$HOME/Library/Application Support/FeishuPersonalAgent"; DB="$H/state.sqlite3"
sqlite3 "$DB" 'PRAGMA user_version'                              # 8
sqlite3 "$DB" 'SELECT COUNT(*) FROM outbox'                      # 与迁移前一致
sqlite3 "$DB" 'SELECT state,COUNT(*) FROM outbox GROUP BY state' # 与迁移前一致
sqlite3 "$DB" 'PRAGMA foreign_key_check'                         # 空
sqlite3 "$DB" 'PRAGMA integrity_check'                           # ok
ls -l "$DB" "$DB".v7-*                                           # 备份 0600、大小非零
feishu-bridge status
feishu-bridge doctor --assert; echo rc=$?
```

---

## 5. 出事时的三个「别做」

1. **别**把备份 `cp` 回去却留着 `-wal`/`-shm`（§2.2 的原因）。这是最容易造成二次损坏的一步。
2. **别**手工 `UPDATE outbox SET state=...` 去"修"一行；要改就改回 v7 重跑迁移，或者新增一条 outbox 行。
3. **别**用旧 release 打开 v8 库"先跑一会儿再说"——它开不了（版本门禁），别为了绕过门禁改 `SCHEMA_VERSION`。

---

## 6. 部署后的真机确认（需要主人在飞书里配合一次）

1. 主人 @ 机器人一条消息 → 消费者 `events say "收到，我看看"` → **群里先出现这句**。
2. 再 `events reply "做完了"` → 出现终回复，且 `inbox.state='reply_queued'→'completed'`。
3. 对第 1 步那条的 mid 执行 `events retract --message-id <mid>` → 飞书侧消失；
   `SELECT reply_id, retracted_at FROM outbox WHERE row_id=?` 两列有值。
4. 网关那条：Mac 上 `feishu-bridge events serve`，Windows 侧带 Bearer 调
   `POST /v1/events/{id}/say`。

判据 1、2 同时成立，才算「先答一句、干完再报」真的通了（这也是 S1 的产品目标）。
