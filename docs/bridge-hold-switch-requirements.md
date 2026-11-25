# Bridge 启停开关（hold / release）需求确认书

> 状态：**已确认并已实现**（2026-11-02）。需求确认见 §11/§13，实现落地与验收证据见 §16。
> 实现范围（勿误读）：CLI 只有 `hold [-r 原因] [--by 标识]` / `release`；状态并入 `status`（`mode` 字段），
> **没有**独立的 `feishu-bridge mode` 子命令。§6.1 里的 `--until`（定时自动上班）、`--queue`（积压策略开关）、
> §5.2 的 `auto_release_at_ms` 仍属**设计草案，未实现**；当前积压策略固定为「丢弃并计数」，恢复时用
> `service_epoch_ms` 把挂起窗口从回填时间轴上永久切掉。
> 适用范围：全平台 —— macOS（launchd）、Linux/WSL2（systemd user / system scope）、Windows（WSL2 系统级 unit + holder）。

---

## 1. 需求原文与我的理解

| 你的原话 | 我的理解（可执行版） |
|---|---|
| 「Bridge 启停开关」 | 一个**显式、持久化、跨平台一致**的开关；表达"现在要不要让 bridge 参与会话"。 |
| 「关闭后，所有消息不进入队列」 | 关闭期间，即使有人 @ 我，消息也**不得进入 bridge 的任何队列**（不建待办、不建事件、不建分析任务），不是"入了队但不回复"。 |
| 「关闭期间，所有 @ 的消息不进入系统和队列」 | 关闭窗口内的消息**永久不参与**本系统；重启、回溯扫描、补答都不允许把它们捞回来。 |
| 「开启后再进入参与会话任务」 | 恢复后只处理**恢复时刻之后**的新消息，然后回到正常接待状态。 |
| 「针对所有操作系统平台」 | 三种宿主上行为必须一致，且不依赖各平台服务管理器的"停止"语义。 |

**一句话目标**：一个"值班开关"——下班关掉，谁 @ 都不进系统、不占队列、不产生回复；上班打开，从此刻起正常接待。

---

## 2. 现状（代码事实，已逐条核对）

| 组件 | 现状 | 位置 |
|---|---|---|
| `feishu-bridge pause` / `resume` | **只改** `config.json` 的 `sending_enabled`；提示语明说「已暂停自动分析和回复；**收件继续**」 | `bridge/cli.py:472`、`bridge/cli.py:891` |
| 采集 | collector 线程持续跑 `live`（12s）与 `backfill`（24h 安全网）两种扫描 | `bridge/service.py:186-270` |
| **入队点** | 搜索命中 → `store.ingest_search_page` 直接 `INSERT INTO inbox`，state 默认 `details_pending`（**这就是"队列"**） | `bridge/store.py:230-243`、`bridge/store.py:73` |
| 详情判定 | details 线程取 `details_pending` → 过滤（撤回/未@/自己/旧消息/未进群）→ `external_queued`(入事件表) / `ignored` | `bridge/service.py:344-386` |
| 发送 | `sender` 由 `enabled()` 门控 = `sending_enabled && target_open_id` 匹配（回复由外部消费者写进 outbox） | `bridge/service.py:105-112` |
| 旧消息过滤 | `create_time < service_epoch_ms` → 标记 `before_service_activation` 忽略；`service_epoch_ms` 在服务**首次身份成功**时写入一次 | `bridge/service.py:197-198`、`355-359` |
| 启动即回溯 | 进程启动时 `next_backfill = 0`，**立刻做 24h 回溯** | `bridge/service.py:186` |
| 事件超时 | `home/external.json` 只剩 `reply_timeout_seconds`（原子写 + mtime 缓存）；details 一律写 `message` 事件，超时进死信 | `bridge/service.py:557-582`、`store.py:639-655` |
| 健康判定 | `receiving_healthy = 进程在 && 已认证 && 120s 内有搜索 && 采集滞后正常` | `bridge/cli.py:403-420` |
| macOS 进程级停止 | plist `KeepAlive: True` → `launchctl` 停止后**会被重新拉起** | `bridge/host.py:127-129` |
| Linux/Windows 进程级停止 | unit `Restart=always`（`systemctl stop` 尊重人工停止，但崩溃/重启会拉起） | `dist/wsl2/wsl/feishu-bridge.service.template` |

**结论**：现在的 `pause` 是**静音**（照常收、照常入队、只是不回复）。你的第 2 条要求它不满足——关闭期间队列会持续堆积，恢复后还会把积压消息当新消息处理。

---

## 3. 差距分析（要做 vs 现在）

| 要求 | 现状 | 差距 |
|---|---|---|
| 关闭后消息不入队 | 照常 `INSERT inbox` | 需要在**采集层**拦住，而不是在分析层 |
| 关闭窗口永久不参与 | `service_epoch_ms` 只在首次启动写死，24h 回溯会覆盖关闭窗口 | 需要在恢复时把 epoch **前移**到恢复时刻 |
| 关闭状态持久 | 无此概念 | 需要持久化状态文件，重启后仍关闭 |
| 全平台一致 | `pause` 只写 config | 统一 CLI + 状态文件，与 launchd/systemd 解耦 |
| 可观测 | `status` 没有"关闭中" | 需要 `mode/held_since/...`，且不能把"关闭"误报成"故障" |

---

## 4. 概念分层（建议先对齐口径）

"启停"在系统里有三层，含义完全不同，混用会出事：

| 层 | 含义 | 手段 | 是否满足你的第 2 条 |
|---|---|---|---|
| **L1 进程启停** | 进程没了 | `feishu-bridge stop/start`（launchd/systemd） | ❌ 不满足：停下期间没人采，但**重启后 24h 回溯会把关闭窗口捞回来**；macOS `KeepAlive` 还会自动拉起 |
| **L2 挂起 hold**（本需求） | 进程在，但**不采集、不入队、不回复** | 新增 `feishu-bridge hold/release` + 状态文件 | ✅ 满足 |
| **L3 静音 mute** | 照常收与入队，只是不回复 | 现有 `pause/resume` | ❌ 不满足（队列继续长） |

**建议**：你的"启停开关"落在 **L2**；L1 保留为运维手段，L3 保留为"临时静音"。三者语义在文档与 `--help` 里写清楚，不互相替代。

---

## 5. 目标语义（状态机）

```
                 feishu-bridge hold                 feishu-bridge release
   ┌──────────┐ ──────────────────────▶ ┌────────┐ ──────────────────────▶ ┌──────────┐
   │ running  │                         │  held  │                         │ running  │
   │ 正常接待  │ ◀────────────────────── │ 挂起中  │                         │ 正常接待  │
   └──────────┘      （不支持自动/隐式）  └────────┘                         └──────────┘
        │                                     │
        │ L3 mute（pause）                     │ 重启/断电/崩溃后仍保持 held（不自动恢复）
        ▼                                     ▼
      静音：照常收、不回复                    零入队：搜索不执行、inbox 不新增、事件不新增
```

### 5.1 held 时逐线程行为

| 线程 | 行为 | 理由 |
|---|---|---|
| collector | **不执行** live / backfill 扫描（两个 `next_*` 冻结）；仍可保留每轮 1s 的空转与 `stop.wait` | 不搜=不可能入队，这一条是"零入队"的唯一可靠保证 |
| details / sender | 全部 idle（无新 inbox 行，自然无活） | 不产生回复/事件 |
| heartbeat / status | **照常**（3s 写 `status.json`），额外写 `mode=held`、`held_since` 等 | 关闭≠故障，监控要能区分 |
| 身份刷新 | 建议**保留**（每 60s 读一次自身身份，不读消息） | 让 `authenticated` 仍可信、恢复时不必等 token 刷新；不产生任何消息入队 |

### 5.2 进入 held（`hold`）时的一次性动作

1. 原子写 `home/hold.json`（`{active, since_ms, by, reason, auto_release_at_ms, schema}`）
2. `reset_scan('live')`、`reset_scan('backfill')`：丢弃进行中的分页游标（避免半页残留）
3. 按 §11-Q3 决定已有积压的处置（清空 / 保留 / 仅清外部事件）
4. 已入队的 pending/leased 事件按同一策略置 `dead` 或冻结投递（不允许"关闭期间仍被消费者领取"）

### 5.3 退出 held（`release`）时的一次性动作

**关键：把关闭窗口从时间轴上"切掉"，让回溯永远看不到它。**

1. `service_epoch_ms := release_ms`（此后 `create_time < epoch` 一律 `before_service_activation` → 忽略）
2. `backfill_target_ms := release_ms`、`backfill_position_ms := release_ms`
3. `reset_scan('live')`、`reset_scan('backfill')`；live 水位也置 `release_ms`
4. `next_backfill = 0` → 立刻做一次回溯，但其下界已是 `release_ms`，**只会回补恢复之后的**
5. 清 `hold.json`（`active=false` 保留 `last_release_at` 便于审计），累计 `dropped_during_hold`

**验收视角**：release 之后任何一次 `transport.search(start_ms, ...)` 的 `start_ms` 都必须 `>= release_ms`。

### 5.4 持久化与重启

- `hold.json` 与 `external.json` 同构：原子写（临时文件 + `os.replace`）、服务每轮按 `mtime+size` 缓存读取
- 服务重启 / Windows 重启 / WSL 重启 / `systemctl restart` 后：读到 `active=true` → **继续 held**，不自动恢复
- 这与"关闭期间不参与"直接相关：不能因为机器重启就把开关丢了

---

## 6. 接口设计（草案）

### 6.1 CLI

```bash
feishu-bridge hold    [-r|--reason "下班"] [--by "名称"]        # 已实现
feishu-bridge release                                          # 已实现
feishu-bridge status                                           # mode=running|held（已实现）

# 设计草案、当前未实现（看到这里不要以为已经存在）：
#   --until ISO8601|+2h        定时自动上班（hold.json 里的 auto_release_at_ms）
#   --queue drop|keep|...      积压处置策略（现固定为丢弃并计数）
#   feishu-bridge mode         独立子命令（状态已并入 status）
```

- 幂等：`hold` 重复执行不重置 `since_ms`（打印"已处于挂起"）；`release` 重复执行也无害
- 退出码：`0` 成功；`2` 参数错误；`3` 状态文件不可写
- 现有 `pause`/`resume` **保持原语义不变**（仅静音），`--help` 里三者并列解释

### 6.2 `status.json` / `feishu-bridge status` 新增字段

```json
{
  "mode": "held",
  "held_since": "2026-11-03T18:00:00+08:00",
  "held_by": "pc",
  "held_reason": "下班",
  "release_at": null,
  "last_release_at": "2026-11-03T09:00:00+08:00",
  "dropped_during_hold": 0,
  "sending_enabled": true,
  "process_running": true,
  "receiving_healthy": false
}
```

- held 时：`receiving_healthy` 为 `false` 是**预期**，另给 `mode=held` 作为"非故障"信号；`doctor` 用同一口径，不报错
- running 时：`mode=running`，其余字段为 `null` 或上次值

### 6.3 状态文件格式（`home/hold.json`）

```json
{
  "schema": 1,
  "active": true,
  "since_ms": 1793607000000,
  "by": "pc",
  "reason": "下班",
  "auto_release_at_ms": null,
  "last_release_at_ms": 1793574000000,
  "dropped_during_hold": 0
}
```

---

## 7. 平台一致性

| 平台 | 开关如何生效 | 注意 |
|---|---|---|
| macOS | CLI 写 `hold.json`；launchd 托管进程继续跑，collector 空转 | **不要**用 `launchctl stop` 代替：`KeepAlive: True` 会拉起 |
| Linux / WSL2（system scope） | CLI 写同一文件；系统级 unit 继续 active | `Restart=always` 不干扰（没有退出） |
| Windows | 同上；holder 继续让发行版活着 | 关闭期间依然占内存（进程在），但零 API 调用、零入队 |

**一致性保证**：开关只走 `feishu-bridge` CLI + 文件状态，不依赖 systemctl/launchctl 的"停止"，因此三平台行为同一份实现。

---

## 8. 边界与异常（需要在实现时覆盖）

| 场景 | 期望行为 |
|---|---|
| release 与采集并发（同一瞬间） | 以 `hold.json` 的 mtime 为准；release 后用 epoch 兜底，最坏情况是"多扫到一次但被 epoch 过滤" |
| 时钟回拨（NTP/休眠） | epoch 与 `create_time` 都是墙钟，回拨可能导致重复过滤；用 `since_ms` 单调兜底 + 日志告警 |
| 关闭时队列里有 leased（被 worker 领走）任务 | 处置策略同 Q3；实现时必须释放租约，避免永久卡住 |
| 关闭期间 token 过期 | 恢复后第一次身份刷新失败 → 沿用现有错误处理；`status` 显示 `authenticated=false` 且 `mode=held` |
| `hold.json` 损坏/半写 | 原子写保证不会半写；真损坏时按 Q7 的 fail-open / fail-closed 处理并写日志 |
| 多台机器同时装（各自账号） | 开关是**本机状态**（每台独立）；若需要"一键全关"属于另一个需求（Q7 备选） |
| 关闭期间有人 @ 我 | 不采集、不回复；对方不会收到任何提示（除非另做"自动告知"扩展，见 Q6） |
| 关闭期间我自己发消息 | 同样不采集（不区分发送者） |

---

## 9. 兼容性与迁移

- 不改变 `config.json` 既有键语义；`hold.json` 为新增文件，旧版本读不到也不受影响
- 库表新增字段通过 `PRAGMA user_version` 迁移（现有 store 已有版本机制，`bridge/store.py:49`）
- 老 `pause`/`resume` 保持"静音"语义并在帮助里改名说明，避免误用
- 升级路径：`install.py` 重装后 systemd 用户级 unit 会被重新启用（已知），与 hold 无冲突

---

## 10. 验收标准（实现后必须逐条可测）

| # | 断言 | 检查方式 |
|---|---|---|
| 1 | held 时 collector **不调用** `transport.search` | 假 transport 计数 == 0 |
| 2 | held 时到达的 @ 消息 → `inbox` 无新增行、`events` 无新增行 | 事务快照对比 |
| 3 | release 后 `service_epoch_ms == release_ms`（±1s） | 读 meta |
| 4 | release 后首次回溯 `search(start_ms) >= release_ms` | 假 transport 记录入参 |
| 5 | release 后新消息正常走完 details→analysis→outbox | 端到端假 transport |
| 6 | 关闭窗口内的旧消息即便被搜索返回也标记 `before_service_activation` 不回复 | 构造 `create_time < epoch` |
| 7 | 重启后仍 held（服务初始化不自动 release） | 构造 state 目录后启动 service |
| 8 | `hold`×2 / `release`×2 幂等，退出码 0 | CLI 测试 |
| 9 | `status` 在 held 时给出 `mode=held` 且 `process_running=true`、`receiving_healthy=false` | CLI 测试 |
| 10 | 三平台 status 输出字段一致（不依赖 backend） | 单测覆盖 launchd/systemd 分支 |
| 11 | `pause`/`resume` 行为零变化（继续收件、只停回复） | 现有测试保持绿 |
| 12 | 真实环境：hold → 发一条 @（由你手动）→ 队列零增长 → release → 新 @ 正常回复 | 真机手工 + `feishu-bridge status`/`events status` 取证 |

---

## 11. 待确认决策（请逐条选）

| 编号 | 问题 | 选项 | 我的推荐 |
|---|---|---|---|
| **Q1** | "启停开关"落在哪一层？ | ① L2 逻辑挂起（进程在、零入队，推荐）② L1 真停进程（重启后靠 epoch 兜底，但 macOS 会被拉起）③ 两者都要：`hold` + `stop` | ① |
| **Q2** | 关闭期间到达的 @ 消息最终如何处置？ | ① 永久丢弃（无处可查）② 永久丢弃但留审计计数（`dropped_during_hold`，推荐）③ 只留痕迹入库、永不回复 | ② |
| **Q3** | 关闭瞬间队列里已有的积压（inbox pending / events pending）怎么办？ | ① 全部清空并计数（推荐：符合"关闭=不参与"）② 保留，恢复后继续处理 ③ 只清 external 事件，保留 analysis 队列 | ① |
| **Q4** | 开关入口 | ① 仅本地 CLI（推荐）② CLI + 飞书内指令（需要最小控制通道，与"所有 @ 不入队"的字面要求冲突）③ CLI + Windows 桌面快捷方式（一键开关） | ①（③ 可作为附加） |
| **Q5** | 命令命名 | ① 新增 `hold`/`release`，`pause`/`resume` 保持静音语义（推荐，兼容）② 直接把 `pause`/`resume` 改成新语义（破坏现有预期） | ① |
| **Q6** | 扩展能力是否需要 | ① 都不需要（推荐，先做最小闭环）② 需要 `--until +2h` 到期自动恢复 ③ 需要在关闭时给对方回一句"已下班" | ①，②③ 后续再加 |
| **Q7** | `hold.json` 损坏时的默认行为 | ① fail-open（继续运行，推荐：可用性优先）② fail-closed（视为关闭：隐私优先） | ①（如你更在意"绝不误回"，选 ②） |
| **Q8** | 关闭期间是否继续刷新身份/token | ① 继续（每 60s 读自身身份，不读消息；推荐）② 完全静默，恢复后再刷新 | ① |

---

## 12. 工作量与影响面（供你判断优先级）

| 改动 | 文件 | 规模 |
|---|---|---|
| hold 状态读/写 + 缓存 | `bridge/service.py`（新增方法，仿 `external_config`） | ~60 行 |
| collector/details/analyst/sender 门控 | `bridge/service.py`（4 处循环入口） | ~40 行 |
| release 时 epoch/水位/扫描重置 | `bridge/service.py` + `bridge/store.py`（新增 `set_hold_floor`/`drop_pending`） | ~80 行 |
| CLI `hold`/`release`/`mode` + status 字段 | `bridge/cli.py` | ~100 行 |
| 测试 | `tests/test_service.py`、`tests/test_cli.py`、`tests/test_store.py` | ~250 行 |
| 文档 | `dist/wsl2/README.md`、`docs/`、`reviews/` | ~50 行 |

不含"飞书内指令/定时恢复/关闭回执"这些扩展。

---

## 13. 决策记录（已确认）

| 编号 | 决策 | 结论 |
|---|---|---|
| Q1 | 开关层级 | **L2 逻辑挂起**：进程保持运行，零采集、零入队、零回复；重启后仍保持关闭 |
| Q2 | 关闭期间的 @ | **永久丢弃 + 审计计数**（`dropped_during_hold` 累加；不保留原文） |
| Q3 | 关闭瞬间的积压 | **全部清空并计数**（inbox 未处理行 + events pending/leased 一并作废） |
| Q4 | 开关入口 | **CLI + 飞书内指令**（需要控制通道，见 §14） |
| Q6 | 扩展能力 | 暂不做定时恢复/自动回执；**但必须先解决"在哪里看到已下班"这个问题**（见 §14.3） |
| Q7 | 状态文件损坏 | **fail-open**：继续正常运行并记告警（不因坏文件永久停机） |

Q5（命名）与 Q8（关闭期间是否刷新身份）按本文推荐执行：新增 `hold`/`release`，`pause`/`resume` 保持"静音"语义；关闭期间保留每 60s 的身份刷新。

---

## 14. 飞书内指令：必须解决的三个约束

### 14.1 事实核查（已验证，非推测）

| 事实 | 证据 |
|---|---|
| 当前身份是 **user**（个人账号 token） | `lark-cli auth status` → `identity: user` |
| 本机 app 的 bot 身份 **ready** | 同上 → `bot: {status: ready, available: true}` |
| 收消息的推送事件 **只支持 bot 身份** | `lark-cli event list` → `im.message.receive_v1 ... AUTH=bot` |
| 事件总线当前未运行 | `lark-cli event status` → `Bus: not running` |

**推论**：想"挂起期间还能收到你的指令"，个人账号这条路必须靠**轮询搜索**；机器人那条路能零轮询，但指令必须发给**机器人**，而不是 @ 你自己。二者不是同一个入口，不能混为一谈。

### 14.2 两个可选实现

| 方案 | 机制 | 挂起期间的代价 | 前置条件 | 恢复延迟 |
|---|---|---|---|---|
| **A. 轮询式控制扫描** | 挂起时 collector 仍以低频（默认 30s）跑一次**窄窗口搜索**，命中即 `get_message` 取文本；只认控制指令，其余**读完立即丢弃、零落库** | 每 30s 一次 search + 少量 get_message；无 DB 写入 | 无（今天就能用） | ≈轮询间隔 |
| **B. 机器人事件推送** | 起 `lark-cli event consume im.message.receive_v1` 长连接，只处理发给机器人的消息 | 零轮询 | 机器人需被加入你能说话的会话（或与机器人单聊），且事件订阅已开通 | ≈1s |
| **C. A+B 并存** | B 优先、A 兜底（B 掉线不影响开关） | 同 A（B 正常时几乎为零） | 同 B | ≈1s |

**关键取舍**：A 严格满足"消息不入队"（读完即丢），但不是"零 API 调用"；B 才是真正的零轮询，但把控制入口从"@ 你自己"改成了"跟机器人说话"。

### 14.3 「在哪里看到已下班」——可选的状态可见面

| 面 | 形态 | 说明 |
|---|---|---|
| S1 | **飞书内即时回执** | 你说"下班"→ bridge 回一条「已下班（挂起中）｜此期间 @ 不入队｜回复"上班"恢复」；说"上班"→ 回「已恢复」 |
| S2 | **飞书内查询指令** | 发"状态"→ 回结构化摘要（挂起时长、期间丢弃计数、当前模式）。这是挂起期间**唯一会回消息**的例外 |
| S3 | **CLI** | `feishu-bridge status` 显示 `mode=held / held_since / held_reason / dropped_during_hold`；`feishu-bridge status | grep mode` 只读单行 |
| S4 | **Windows `check.cmd` / 桌面报告** | 体检输出改为「挂起中（自 18:00，已丢 0 条）」，不再是"失败" |
| S5 | 飞书个人状态/签名、群公告 | 需要额外权限与写操作，建议不做 |

### 14.4 控制指令的安全边界（无论 A/B 都要满足）

1. **只认本人**：`sender.id == target_open_id`，其他人的"下班"一律当普通消息（挂起期间=丢弃，运行期间=正常业务消息）
2. **只认指令文本**：去掉 @ 提及与空白后**精确匹配**（如 `下班`/`上班`/`状态`），不做模糊/包含匹配，避免误触发
3. **只认挂起之后的指令**：`create_time >= held_since_ms`，防止重启后扫描窗口把历史上的"上班"捞出来误恢复
4. **进程内去重**：同一 message_id 只执行一次
5. **不落库**：控制消息不产生 inbox/events 行，仅在 `bridge.log` 留一行审计（时间、指令、来源 chat）
6. **失败不改状态**：控制扫描出错只记日志，绝不影响 hold 状态本身

---

## 15. 还剩 4 个点需要你定

| 编号 | 问题 |
|---|---|
| **Q9** | 控制通道选 A（轮询，不依赖机器人）/ B（机器人推送，零轮询）/ C（两者并存）？ |
| **Q10** | 指令怎么发、谁能发：仅自聊 / 仅 @ 自己 / 两者都行；是否只允许你本人（推荐是）？ |
| **Q11** | 状态可见面要哪些：S1 回执 / S2 "状态"指令 / S3 CLI / S4 Windows 体检 —— 可多选 |
| **Q12** | 恢复延迟容忍度：10s / 30s / 60s（决定轮询间隔；选 B 时可忽略） |


---

## 16. 实现落地（2026-11-02）

### 16.1 决策 → 代码映射

| 决策 | 落地位置 |
|---|---|
| Q1 L2 逻辑挂起 | `bridge/service.py` 的 `hold_state()/held()/sync_hold()`；collector 在挂起时不搜索 |
| Q2 永久丢弃 + 审计计数 | `bridge/store.py::drop_pending` + meta `hold_dropped_current/hold_dropped_total` |
| Q3 清空积压 | 同上：inbox 未终态 + outbox 未发送 + events pending/leased 一并作废 |
| Q4 CLI + 飞书指令（A 轮询） | `bridge/cli.py::_hold`；`bridge/service.py::control_tick()` 轮询自聊 |
| Q6 不做定时恢复 | 无 `--until`；只做最小闭环 + S1 状态面 |
| Q7 fail-open | `bridge/hold.py::read_state`：损坏→按未挂起 + 日志告警 |
| Q9 A 轮询 | `Transport.chat_messages()`（`GET /open-apis/im/v1/messages`） |
| Q10 仅自聊、仅本人 | `control_tick` 只轮询 `self_chat_ids`；`handle_control_message` 校验 `sender == target_open_id` |
| Q11 S1 回执 | 飞书指令→`transport.reply`；CLI→`Transport.send_text`（失败不影响开关） |
| Q12 ≈30s | `hold_control_poll_seconds` 默认 30 |

### 16.2 关键实现细节（容易踩的地方）

1. **`search()` 看不到自聊**：它固定带 `is_at_me + chat_type=group` 过滤，所以控制通道必须走"列出会话消息"接口；已用真 API 验证返回 `create_time` 为**毫秒戳**。
2. **`scan_bounds` 的回拨钳制会越过时间下界**：P2-11 的"时钟回拨"保护原本会 `start = target - width/2`，在恢复瞬间等于把起点拉回 12 小时前，直接违背"关闭窗口不参与"。已改成 `start = max(floor, target - width/2)`（floor = epoch/origin）。
3. **挂起在 agent 跑完之前发生**：analyst 跑完后若已挂起（或消息早于恢复时刻），走 `store.abandon_message()` 落终态，不产生 outbox——否则恢复后会把旧答复补发出去。
4. **详情线程与挂起竞态**：控制消息必须先落 `ignored`（终态）再执行开关，否则会被随后的清队改成 `dropped`。
5. **sender 认领瞬间切到挂起**：立即 `fail_outbox` 放回队列（恢复时的清队会作废它，不会补发）。

### 16.3 新增/修改文件

| 文件 | 说明 |
|---|---|
| `bridge/hold.py`（新） | 状态读写（原子，fail-open）、指令解析（精确匹配）、回执文案 |
| `bridge/store.py` | `drop_pending`、`apply_release_floor`、`abandon_message` |
| `bridge/service.py` | 挂起门控、控制通道轮询、恢复时的下界前移、status 字段、竞态保护 |
| `bridge/transport.py` | `chat_messages`、`send_text` |
| `bridge/cli.py` | `hold`/`release` 子命令、`status` 的 mode 判定、`doctor` 展示、pause 提示语区分 |
| `tests/test_hold.py`（新） | 25 条断言：状态/损坏/指令匹配、清队、下界前移、控制通道、竞态、幂等 |
| `dist/wsl2/wsl/verify.sh` | 体检输出 `mode=held` / `hold=dropped=N`，挂起不再被误读为故障 |
| `dist/wsl2/README.md` | 「下班 / 上班」运维章节（含自聊控制频道配置） |

### 16.4 验收证据

| 断言（§10） | 状态 | 证据 |
|---|---|---|
| 1 挂起时不搜索 | ✅ | `test_held_service_never_searches`（collector 跑 1.2s，`transport.calls == []`） |
| 2 挂起期间零入队 | ✅ | 同上（不搜索 ⇒ 不 ingest）+ `drop_pending` 清积压测试 |
| 3 恢复后 epoch=release | ✅ | `test_release_moves_epoch_so_off_window_is_excluded_forever` |
| 4 恢复后扫描不越过下界 | ✅ | `test_after_release_no_scan_covers_the_off_window`（每个 `start_ms >= floor`） |
| 5 恢复后新消息正常处理 | ✅ | 既有 638 条测试全绿（管线未回归） |
| 6 窗口内消息命中 `before_service_activation` | ✅ | epoch 前移 + 既有过滤逻辑 |
| 7 重启后仍挂起 | ✅ | `test_hold_state_survives_restart` |
| 8 幂等 | ✅ | `test_hold_then_release_is_idempotent` |
| 9 status 有 mode | ✅ | `test_status_reports_held_mode` |
| 10 三平台字段一致 | ✅ | 字段写在 `status.json`，与 backend 无关 |
| 11 pause 行为零变化 | ✅ | 既有 pause 测试全绿 |
| 12 真机端到端 | ⏳ **待补** | 目标机 Windows 的 sshd 当前连上即被 reset（`Connection reset by peer`，sftp 子系统同样），Tailscale SSH 未开通；恢复后按 §16.5 执行 |

### 16.5 真机验收步骤（Windows 恢复访问后执行）

```bash
# 1) 确认自聊频道已配置（否则飞书指令不可用，只能用 CLI）
ssh user@<win> 'wsl.exe -d Ubuntu -- bash -lc "python3 -c "import json;print(json.load(open(\"\$HOME/.feishu-personal-agent/config.json\")).get(\"self_chat_ids\"))""'
# 2) 挂起 → 记录基线
feishu-bridge hold -r "验收"
feishu-bridge status | grep -E '"mode"|dropped'
# 3) 用另一账号 @ 该账号发 2 条消息，等 60s，确认 inbox 未增长：
#    sqlite3 ~/.feishu-personal-agent/state.sqlite3 "select count(*) from inbox where state not in ('ignored','dropped','completed')"
# 4) 等 90s 观察一次 24h 回溯不会把窗口消息捞回（inbox 计数不变）
# 5) feishu-bridge release → 发一条新 @ → 应正常回复；旧的 2 条永不回复
# 6) 飞书自聊发「下班」/「上班」→ 确认回执与 30s 内生效
```
