# 设计稿：两种身份模式（`as_user` / `as_bot`）

> 状态：**已实现 PR-1/PR-2**（2026-11-20）；PR-3（安装/文档）与真机验收待做。真机在 Windows 开发机。目标：**功能完全一样，只是身份实现不同**。
> **原则（主人 2026-11-20，加粗记住）：一台机器只能有一种身份模式，`as_user` 与 `as_bot` 不允许并存。**
>
> 主人 2026-11-20 定调：用设计模式把"实例化"这件事做对——`as_user` 与 `as_bot` 是两个具体实现类，
> 其余 80% 的逻辑（出站队列、事件、任务账本、审批、干活端、体检、安装）**一行都不该知道身份这回事**。

---

## 0. 现状与事实

| 项 | 现在（`as_user`） |
|---|---|
| 收消息 | 定时**搜索**（`transport.search`，时间窗 + `@我`），个人账号没有事件订阅 |
| 发消息 | `lark-cli` 以**用户 token** 回帖/私聊 |
| "本人"判定 | `config.target_open_id` |
| 命令通道 | `config.self_chat_ids` 白名单 + `own_message` 守卫 |
| 署名 | 安装时配置的 `signature` |

**关键事实（今天在真机上看到）**：同一套 `lark-cli` 同时具备两种身份

```
Bot identity:  available: true
User identity: available: true
```

所以 `as_bot` **不需要另造一套 CLI**，只是换 token 类型与几个 API。

---

## 1. 设计模式：Abstract Factory + Strategy

```
bridge/identity/
    base.py      Identity（抽象基类 / 接口）
    user.py      UserIdentity   ← 现有逻辑搬进来，行为不变
    bot.py       BotIdentity    ← 机器人身份（长连接收 + bot token 发）
    factory.py   build_identity(config)   ← 按 config.identity.mode 实例化
```

- `service` / `collector` / `sender` 只依赖 `Identity` 接口
- **不允许**在业务代码里出现 `if mode == 'bot'`（一旦出现，就说明这个差异应该收进实现类）
- 平台差异（mac/Windows）与本设计**正交**：身份和操作系统是两个独立维度

### 接口契约（唯一 6 个方法）

| 方法 | 语义 | `as_user` | `as_bot` |
|---|---|---|---|
| `start(on_message)` | 启动收消息通道 | 启动搜索轮询 | 建立长连接（WebSocket），事件回调里交给 `on_message` |
| `stop()` | 停通道 | 停轮询 | 断开长连接 / 停 sidecar |
| `reply(original, body, uuid)` | 回帖 | 用户身份回复 | 机器人身份回复（`POST /im/v1/messages/{id}/reply`） |
| `send(chat_id, body, kind)` | 群发 / 私聊 | 用户身份 | 机器人身份 |
| `whoami()` → `Actor` | `{mode, open_id, display, owner_open_ids}` | 用户 open_id | 机器人 `app_id` + 配置的 owner |
| `describe()` → 行列表 | 体检用的只读行 | `mode=user`、`identity=<打码>` | `mode=bot`、`app_id=<打码>`、`channel=ws` |

**默认实现放在 `Identity` 基类里**（能共用的都共用）：

- `is_owner(sender)`：`sender in owner_open_ids`（两种模式同一判据）
- `is_mentioned(message)`：`as_user` 判"提到本人"；`as_bot` 判"提到机器人"——**实现不同，语义相同**（都是"有人在叫它"）
- `needs_self_guard()`：`as_user=True`（别回自己的话）、`as_bot=False`（机器人不会自己说话）——**差异做成默认实现，不进业务代码**

---

## 2. 收消息：长连接（主人印象正确，多数机器人就这么做）

两种可选实现，**本稿推荐 B**：

### A. Python 进程内长连接（`lark-oapi` 的 ws client）

- 优点：单进程、无跨语言
- 代价：引入新依赖（`lark-oapi`），且它的 ws 客户端能力与 `lark-cli` 的封装有重叠

### B. Node sidecar（推荐，复用现有 lark-cli/Node 栈）

```
[lark-cli/Node sidecar] --WS--> 飞书
        |
        |  POST http://127.0.0.1:<port>/v1/ingest   （仅回环，Bearer token 复用 gateway token）
        v
[bridge.service] → details → events → …（与 as_user 完全相同的下游）
```

- sidecar 只做三件事：**建长连接、把事件原样转发、断线重连 + 心跳**
- 桥不需要新依赖；ingest 入口复用现有 `bridge/gateway.py` 的鉴权与路由（新增一个只回环可用的端点）
- 断线期间漏掉的消息：**用搜索 API 做对账回填**（`as_bot` 同样能按时间窗搜到自己被 @ 的消息），
  与现有 collector 的"水位 + 回填"机制复用同一套逻辑

**幂等**：事件/消息 id 作为去重键，落 inbox 前查重（现有 `used_ids` 机制直接复用）。

---

## 3. 配置

```jsonc
{
  "identity": {
    "mode": "user" | "bot",              // 唯一开关
    "owner_open_ids": ["ou_..."],        // 谁能批准、谁能发命令（bot 模式必填；user 模式默认= 本人）
    "command_channel": "self_chat" | "dm",  // user: 白名单会话；bot: 与机器人的私聊
    "bot": {"app_id": "cli_…", "app_secret_from": "lark-cli|env|file", "channel": "ws"}
    // 没有 per_chat_mode，也没有"两个身份一起开"的开关：一台机器一种模式（见 §5）
  },
  "signature": "…"                        // 两种模式共用
}
```

- 迁移：老配置没有 `identity` 块 → 视为 `{"mode": "user"}`（零迁移）
- `doctor` 增加一行 `identity`（模式 + 打码的身份 + 通道健康）

---

## 4. 数据模型：**零迁移**

`inbox / events / outbox / tasks` 全部不动：

- "谁说的"由**这台机器的 `identity.mode`** 唯一确定（一台机器只有一种模式）
- 机器人模式下，`tasks.executor` 仍记"哪台机器干的"；回执署名沿用 `signature`
- 万一将来要同机双模式，再加列（现在不做，避免无依据的迁移）

---

## 5. 一台机器只有一种身份（硬原则）

**不允许并存，也不做"按群路由"这种半并存。** 理由：

- 两套身份同时在线，同一个群里必然**双答**——这是事故，不是功能
- 并存会让"这句话是谁说的"变成需要推理的问题；审计、署名、回执全部失真
- 想换身份就**换配置 + 重启**，这不是频繁操作

**落地方式（启动即失败，不静默）**：

1. 配置只接受 `identity.mode` 的一个取值；出现 `user` 与 `bot` 同时配置的痕迹（例如 legacy 的 `lark_binary` 用户态 + `bot.app_id` 同时存在）→ **启动失败**并说明要删哪个
2. `doctor` 输出 `identity.mode`，并在配置矛盾时给 fail
3. 安装器按模式只生成对应的一套消费者与通道；**不会**两套都装
4. 同一个飞书账号/应用不要在两台机器上以**同一种模式**同时跑（那也会双答）——`doctor` 提示，运维文档写清

## 6. 验收判据（两种模式各跑一遍，输出可对照）

| # | 判据 | user | bot |
|---|---|---|---|
| 1 | 群里被 @ → 回话，挂在原消息下 | ✅ | ✅ |
| 2 | 写活 → 任务卡 → 批准 → 干活端 → 产物 + 回执 | ✅ | ✅ |
| 3 | 只读请求由回话端直接答（不建任务） | ✅ | ✅ |
| 4 | 回话端写文件被沙箱拒（角色边界） | ✅ | ✅ |
| 5 | `doctor --assert` rc=0，含 `identity` 行 | ✅ | ✅ |
| 6 | 断线/重启后不重复回复（幂等键） | ✅ | ✅（重点验长连接重连） |
| 7 | 找不到"自己的消息"时不回（`needs_self_guard`） | ✅ 必须 | 不适用（`False`） |

---

## 7. 风险

| 风险 | 处置 |
|---|---|
| 长连接断线漏事件 | 重连 + 搜索对账回填（同一套水位机制） |
| 飞书应用权限不足（读群消息/回复） | 安装向导列出最小权限清单；`doctor` 直接给出缺哪个 scope |
| 双身份双答 | 设计上不允许并存：一台机器一种模式，配置矛盾即启动失败 |
| 机器人发帖与"人"混在一起，同事观感变化 | 署名与头像区分；文档写清 |
| 合规 | `as_bot` 是正规应用形态；`as_user` 保留为"个人自用" |

---

## 8. 交付拆分（3 个 PR + 收尾）

| PR | 内容 | 判据 |
|---|---|---|
| PR-1 | `identity/` 抽象 + `UserIdentity`（现有逻辑搬家，行为不变）+ factory + service 只依赖接口 | 现有 1597 条测试全绿（行为零变化） |
| PR-2 | `BotIdentity` + sidecar 长连接 + ingest 回环端点 + 幂等/对账 | 本地假飞书事件源：事件到达 → 回话/派活；重连不重发 |
| PR-3 | 安装/配置/doctor/文档（两模式）+ 「一台机器一种身份」的启动自检 | 两台机器各跑 §6 七条（各自一种模式） |
| 收尾 | 真机验收（先开发机，再一台同事机）+ HISTORY | 判据可对照 |

**估时：5 人日**（PR-1 1.5、PR-2 2、PR-3 0.5、收尾/真机 1）。并存方案被否掉，省 0.5–1 天。

---

## 9. 要主人拍板的四点

1. **机器人应用**：复用现有那个应用（`cli_aaa…`，已有 bot 身份），还是新建一个专用应用？
2. **第一台验哪台**：建议先开发机（WSL），不影响同事。
3. **命令通道**：机器人模式下"上班/下班/状态"由谁发？建议=**主人与机器人的私聊**。
4. ~~要不要同机双模式~~ → **已定：不允许并存**（主人 2026-11-20）。本稿不再有该选项。
