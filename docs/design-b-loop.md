# B 档剩余设计（S3a 干活端起）

> **这份文档只讲还没做的。** S1（出口唯一）与 S2（任务卡与审批）已经落地并上线，
> 现在该怎么做以 [CONTRACTS.md](../CONTRACTS.md) 为准；为什么这么定见
> [docs/HISTORY.md](HISTORY.md)。本文件剩余部分是 S3a（mac 干活端）、S3b（Windows 桌面版）、
> S4 余项（白名单配置化与 executor 审计）、S5（平台验收与文档）的设计与验收判据。
>
> **一句话**：活一旦被批准，要由**主人自己那台机器上、他自己正在用的 DSH**干完，结果只能从桥回群。

## 0. 剩下的问题

S1 让"出口唯一"成立，S2 让"活能被批准"。现在缺的是**干活的那一端**：

- 批准后的任务停在 `running`，没有人去执行它（`tasks.executor` 就是留给它的字段）；
- 桥的消费者（`dsh-main`）窄权限、短超时，只能"听懂 → 回一句 → 起草卡"，干不了长活；
- 主人手里的工作台（mac 的 `dsh web` / Windows 桌面版）能力齐全，但还不知道桥上有活等它。

## 1. 剩余目标

1. 一件被批准的活，从桥交给干活端，干完把结果**从桥**回群（带引用、可撤回、可审计）。
2. 干活的现场是**主人自己正在用的工作台**：mac 是浏览器里的 `dsh web`，Windows 是桌面版。
3. 出了错有话说：失败、超时、被锁、被睡眠打断，都要回群说清楚，不许静默。
4. 两端（mac / Windows）语义一致：同一组判据，同一个答案。

**仍是非目标**：工作台插件面板；跨人调度；并发多任务与 MCP 白名单细化；Windows 原生（非 WSL2）桥。

## 2. 术语（都用日常词，不造新名词）

| 词 | 指什么 |
|---|---|
| **桥** | `feishu-bridge` 服务：唯一入口（收消息）、唯一出口（发消息）、唯一账本、唯一授权闸门 |
| **回话端** | 现在的 `dsh-main` 消费者：窄权限、快。只做"听懂 → 回一句 → 起草任务卡" |
| **干活端** | 新增的 `dsh-worker` 消费者：宽权限、长超时。只做"把批准的活干完并回话" |
| **工作台** | 主人自己正在用的那个 DSH 窗口。mac = `dsh web`（浏览器）；Windows = `DeepSeek Harness.exe`（桌面版） |
| **任务卡** | 一条活的草案：原文 + 我理解要做什么 + 计划 + 要碰什么 + 风险 + 验收标准 |
| **首答 / 追答** | 对同一条飞书消息的第 1 条回复 / 之后的第 2、3 条（结果、更正） |
| **批准** | 主人在**私聊**里**引用回复**任务卡；引用是结构信号，没引用的消息永远不算答复 |

## 3. 架构

```
飞书群/私聊
   │  collector → details → events(message)
   ▼
┌─────────────────────────────────────────────────────────────┐
│ 桥（macOS launchd / Windows WSL2 systemd）                    │
│   · 唯一入口：事件、租约、幂等                                 │
│   · 唯一出口：outbox → sender → verify（署名/@/脱敏/配额/撤回） │
│   · 唯一账本：tasks（谁在做、做到哪、证据）                     │
│   · 唯一授权闸门：只有主人的引用回复能把任务从 open 推到 running │
└───────┬───────────────────────────────────┬─────────────────┘
        │ message/attention                  │ work
        ▼                                    ▼
   回话端 dsh-main                        干活端 dsh-worker
   （240s，无子代理/MCP）                 （1800s，工作台权限档）
        │                                    │
        │ 起草任务卡 + ask_owner              │ 工作台适配器
        ▼                                    ▼
   私聊卡片 → 主人引用回复 ──▶ 批准    ┌──────────────────────────┐
                                       │ mac : dsh headless       │
                                       │       DSH_HOME=~/.dsh    │→ 主人的 web GUI
                                       │ win : cmd /c dsh.cmd …   │
                                       │       DSH_HOME=C:\…\.dsh │→ 主人的桌面版
                                       └──────────────────────────┘
```

**分工铁律**

1. 回话端永不执行长活；干活端永不自己发飞书，只能调 `feishu-bridge events say`。
2. 干活端启动的 DSH 必须和主人工作台**同一个 `DSH_HOME`**。否则任务落进没人看得见的会话——
   这是最容易悄悄踩的坑（真机上第一轮裸跑就撞上 `MISSING_CREDENTIAL`，两套 home 立刻现形）。
3. 一条活的"做到哪"只有一个答案：`tasks` 表。任何一方发言前先读它。

## 4. 平台矩阵（同一个机制，两种工作台）

| | macOS（主人） | Windows（同事） |
|---|---|---|
| 桥 | launchd 用户单元，`~/Library/Application Support/FeishuPersonalAgent` | WSL2 + systemd 系统单元，`~/.feishu-personal-agent`（**原生 Windows 不支持**） |
| 工作台 | 浏览器打开 `dsh web`（默认 3080，token URL） | 桌面版 `DeepSeek Harness.exe`（自带 DSH，独立 home） |
| 工作台 home | `~/.dsh` | `C:\Users\<用户>\.dsh` |
| 干活端 CLI | 全局 `dsh`（`~/.npm-global/bin/dsh`） | 桌面版自带：`<安装目录>\resources\runtime\cli\bin\dsh.cmd` |
| 启动方式 | `direct` | `cmd-interop`（WSL 里 `cmd.exe /c …`，路径必须是 Windows 形态） |
| 凭证 | `~/.dsh/.credentials.yaml`（overlay 三行恢复） | `C:\Users\<用户>\.dsh\.credentials.yaml`（同一套 overlay，真机已验证） |
| 会话归属 | `~/.dsh/sessions/<slug>/session-*` | `C:\Users\<用户>\.dsh\sessions\<slug>\session-*` |

> 形状对照（真机已验证，2026-11-16）：Windows 上 `dsh web` 也能跑，但**只能从 Windows
> 浏览器用 WSL 的 IP 访问**（`dsh web` 拒绝通配绑定，而 Windows→WSL 的 localhost 转发只对
> 通配绑定生效）；而同事日常用的是桌面版，所以 Windows 的默认形态定为 `dsh-desktop`，
> `dsh-web(WSL)` 只作为没有桌面版时的退路。

## 5. 数据模型

### 5.3 events：`work` 事件 + 角色路由

```sql
-- consumers 表加一列（迁移时默认 replier，保持旧行为可解释）
ALTER TABLE consumers ADD COLUMN role TEXT NOT NULL DEFAULT 'replier';
```

- `EVENT_KINDS` 加 `work`：payload = `{task_id, chat_id, origin_mid, body, hints, acceptance, reply_with}`
- **谁能领什么，由事件类型决定，不看消费者名字**：
  - `message` / `attention` → `role=replier`
  - `work` → `role=worker`
  - 现有"任何消费者可领任何事件"的行为要收掉——这是当前最容易出事故的一处（回话端抢到活，
    240 秒后被砍断，然后记账记成"失败"）。

### 5.4 配置：`workbench` 块（每台机器一次）

```jsonc
{
  "workbench": {
    "kind": "dsh-web" | "dsh-desktop",
    "cli": "/Users/<用户>/.npm-global/bin/dsh",       // win: D:\\...\\runtime\\cli\\bin\\dsh.cmd
    "home": "/Users/<用户>/.dsh",                     // win: C:\\Users\\<用户>\\.dsh
    "launch": "direct" | "cmd-interop",
    "workspace": "/Users/<用户>/Documents/ai-market-team",
    "timeout_seconds": 1800,
    "credential_overlay": "auto"
  }
}
```

## 6. 状态机与失败语义

```
                     ┌──────────── 主人「不做」────────────┐
                     ▼                                    │
  @消息 ──▶ [草案 open] ──主人引用回复「做」──▶ [running] ──成功──▶ [done]
                 │                                │  └──失败/超时──▶ [failed]
                 │ 30 分钟无答复                   └──要权限/被锁──▶ [blocked] ──答复──▶ [running]
                 └──▶ 保持 open（群里说一句「等你点头」）
```

**每一种坏结果都必须有话说**（这是本仓的铁律：静默降级不是修复）：

| 情形 | 记账 | 回群 |
|---|---|---|
| 会话被工作台占用（`session.lock` 有主） | `blocked` + open_question | 「这个会话你正开着，我不敢动；关掉我再来」 |
| 干活超时 | `failed`，错误写进 note | 「超时了，卡在 <步骤>」 |
| 凭证失效（`MISSING_CREDENTIAL`） | `failed` | 「我这边的登录好像过期了」 |
| 进程被杀（Windows 睡眠/重启） | `failed`（检测子进程消失） | 同上，并说明「机器睡过去了」 |
| 任务包本身不可能（缺权限/缺接口） | `blocked` | 说清缺什么、下一步找谁 |

### 6.1 真机验证过的失败语义（2026-11-17，Windows 开发机，dsh 0.2.1-rc.5）

| 情形 | 实测结果 | 干活端该怎么做 |
|---|---|---|
| 同一会话被占用 | 第二条 `--session-id` **立刻失败**：rc=1，stdout 一行 `{"type":"error","message":"session \"…\" is already owned by an active write handle"}`，stderr 同一句 | 匹配这句话 → `blocked` + 问主人「这个会话你正开着，关掉我再来」。**不要**用 `session.lock` 文件是否存在来判断：锁是操作系统写句柄，文件是空的，残留无害 |
| 机器睡眠 / WSL 被关掉 | 进程直接消失：NDJSON 停在 `{"type":"status","phase":"step_start",…}`，**没有 `final` 事件**，也没有任何退出码被交出来 | 以「没有 `final`」为失败判据 → `failed` 并回群说「机器睡过去了」；不要静默重试 |
| 被中断后能否续跑 | 会话文件完好（`session.v4.jsonl.zstd`），`--session-id` 可继续，模型自己说出了「上一轮被 SIGTERM 打断」 | 续跑是可行恢复路径；汇报里写清「上次被打断，我从这里接上」 |
| 进程树 | `wsl --shutdown` 后无残留 dsh 进程；重启后残留的 `session.lock` 不阻塞新会话 | 取消/超时要自己收进程树：WSL 用 `killpg`，Windows 用 `taskkill /T` |

> 另一个运维事实：发行版重启会清空 `/tmp`（本机实测：放在 `/tmp` 的 NDJSON 重启后消失）。
> 干活端的会话日志与临时产物**不要放 `/tmp`**，要放持久目录。

## 9. 干活端与工作台适配器

```python
# 伪代码：唯一入口，两种平台只是配置不同
class Workbench:
    def prepare(self):                   # 启动时自检，错就失败并说明下一步
        - cli 可执行、--version 能答
        - home 存在且有 .credentials.yaml 或 DSH_API_KEY
        - workspace 可写
        - 与工作台同一个 home（配置项门禁）
    def run(self, prompt, session_id=None) -> NDJSON 事件流
        - direct     : subprocess([cli, 'headless', '--patch', overlay, '--json', '-'])
        - cmd_interop: subprocess(['cmd.exe','/c', cli, 'headless', ...], cwd=win_path)
        - DSH_HOME = self.home（Windows 形态）
```

**必须处理的四件事**

1. **凭证**：每次运行写一个 0600 的 overlay（`credentials`/`authorization`/`deepseek-account`
   三行 `disabled: false`），跑完立刻删——复用 `dsh-consumer.py` 的现成做法。
2. **路径映射**：WSL 里的 `/mnt/c/...` ↔ Windows `C:\...`；`--patch`、cwd、`DSH_HOME` 全用
   Windows 形态；写一个函数 + 单测钉住。
3. **互斥**：同一会话不允许并发；检测 `session.lock`/子进程 —— 冲突就 `blocked`，不重试硬闯。
4. **进程树**：超时/取消时要能收掉整棵树（Windows 上 `taskkill /T`，WSL 上 `start_new_session` +
   `killpg`），否则会留下孤儿 DSH 进程继续烧 token。

## 10. 环境准备清单（每台机器一次，逐条可复跑）

### macOS（主人）

| # | 做什么 | 判据 |
|---|---|---|
| 1 | 桥 + 回话端（已有） | `feishu-bridge status` → `receiving_healthy: true` |
| 2 | 工作台 `dsh web` 可用 | 浏览器打开 token URL，能新建会话并对话 |
| 3 | 装干活端单元 `io.feishu-agent.dsh-worker` | `launchctl print gui/$UID/io.feishu-agent.dsh-worker` 有 pid |
| 4 | `workbench` 配置 + 自检 | `feishu-bridge workbench check` → 全 PASS |
| 5 | 端到端一次真活 | 群里 @ → 私聊批准 → 干活 → 群里一条带引用的追答 |

### Windows（同事机，WSL2 全内）

| # | 做什么 | 判据 |
|---|---|---|
| 1 | 桥 + 回话端（已有） | `feishu-bridge status`（WSL 内） |
| 2 | 桌面版已装、`C:\Users\<用户>\.dsh` 存在 | `where` 到 `dsh.cmd`，`dsh.cmd --version` 与桥期望一致 |
| 3 | 干活端 unit（systemd，WSL） | `systemctl status io.feishu-agent.dsh-worker` → active |
| 4 | `workbench` 配置（kind=dsh-desktop, launch=cmd-interop） | `feishu-bridge workbench check` → 全 PASS |
| 5 | 会话可见性（人看一眼） | 干活产生的新会话出现在桌面版会话列表里 |
| 6 | 端到端一次真活 | 同 mac 第 5 条 |

### Windows 开发机（只用于验证，不是同事生产机）

开发机不需要完整部署：**只装"执行层 + 工作台"，不装桥、不接飞书身份**（否则同一台机器上会出现
第二个"我"在群里说话，而且凭据要复制一份）。桥仍用主人 Mac 上那套，开发机通过 SSH / HTTP 网关
接受任务。

| # | 做什么 | 判据 |
|---|---|---|
| 1 | WSL2 + 发行版 + 普通用户 + systemd | `wsl -l -v` → VERSION 2；`ps -p 1 -o comm=` → `systemd` |
| 2 | Node ≥22 + `npm i -g @deepseek-ai/dsh@<与 mac 相同版本>` | `dsh --version` 两端同一个值 |
| 3 | 工作台（没有桌面版时）= WSL 的 `dsh web`，Windows 浏览器访问 | Windows 侧 `curl <WSL IP>:3999/` → 401；带 token → 303 |
| 4 | 不装桥 | 开发机上没有 `~/.feishu-personal-agent`，`feishu-bridge status` 不存在 |

**两条真机踩过的坑**（2026-11-17，开发机）：

- `npm install -g @deepseek-ai/dsh` 会报 `ETARGET: No matching version found ...@*` —— 该包迄今
  发布的**全部**版本都是预发布，`*` 解析不到任何版本。必须写明确版本或 `@latest`。
- 桌面版自带的 `dsh.cmd` 与 WSL 里 npm 全局装的 `dsh` 是**两套安装、两套 home**。开发机没装
  桌面版时，工作台只能是 WSL 的 `dsh web`；验收时先确认 `DSH_HOME` 指向哪个，别把会话写进
  没人看得见的那套。

**自检要输出人话**，例如：

```
workbench check
  kind            dsh-desktop                 PASS
  cli             D:\…\dsh.cmd (0.2.1-rc.5)   PASS
  home            C:\Users\<用户>\.dsh        PASS
  credentials     stored (.credentials.yaml)  PASS
  workspace       C:\Users\<用户>\dsh-workspace PASS
  same_as_gui     ✓ 与桌面版同一 home          PASS
  interop         cmd.exe 可调起               PASS
```

### 10.1 桌面版真机事实（2026-11-17，开发机实测）

1. **桌面版自带一套 CLI，版本与 WSL 里 npm 装的那套不同**：桌面版（装在用户
   `%LOCALAPPDATA%\Programs`）自带 CLI 是 `0.2.2-rc.3`，WSL 里钉的是 `0.2.1-rc.5`。
   桌面版会自己升级 → 适配器**只依赖 `--json` / `--session-id` 契约，不钉版本号**；
   `workbench check` 要把两端版本都打出来，不一致时点名提醒，但不阻断。
2. **同一个 home 里同时存在 `profiles/desktop` 和 `profiles/headless`**：桌面版和外部 CLI
   可以共用 `%USERPROFILE%\.dsh`，这正是"外部建的会话桌面版能看到"的前提。切换入口只换
   `DSH_HOME`，不换存储。
3. **从 WSL 调 Windows 原生 CLI 时，环境变量不会自动跨过去**：实测不加
   `WSLENV=DEEPSEEK_API_KEY` 就报 `MISSING_CREDENTIAL`，加上即成功。`cmd-interop` 适配器
   必须显式设置 `WSLENV`（多条用 `:` 分隔），这是硬要求，不是可选优化。
4. **凭据存在同一个文件里，但 headless 要先"打开"credentials 服务**：桌面版 home 的
   `.credentials.yaml` 是两边共用的存储，`refs` 是明文的名字→密钥映射。把
   `DEEPSEEK_API_KEY` 写进 `refs` 之后，桌面版自带的 CLI **不设任何环境变量**也能跑通
   （实测 `final: "STORE-KEY-OK"`，reason `completed`）。之前 `MISSING_CREDENTIAL` 的真正
   原因是 headless 的 `--patch` 覆盖层里**没开 credentials 服务**——必须补上
   `- id: credentials / disabled: false`（连同 `authorization`、`deepseek-account`），
   否则它根本不去查这份存储。反过来，桌面版**不会**读 `/etc/feishu-bridge/dsh.env`，
   它是被人打开、从自己的 store 取密钥的。
5. **外部（headless）创建的会话会出现在侧边栏，但归在「未分组」下**（这条决定 B 档能做到哪）：
   侧边栏的项目分组来自 `storages/workspace.json`（unit version 2）注册表，而 headless profile
   的 bundle 是 `dsh-base + dsh-headless`，**不含 `dsh-workspace`**，所以 CLI 建的会话不会被
   挂进任何项目。但注册表之外还有一条兜底：**没有归属的会话统一进「未分组」列表**，照样
   能点开、能接着聊。实测：桌面版侧边栏里 5 条会话全部落在「未分组」，其中 4 条是外部 CLI
   建的（含完整回合，10–30 KB），可打开、可续聊。
   → B 档的"干活端建会话 → 人类在工作台能看到"**是成立的**，只是**不在项目分组里**；
   不要为了让它进项目而反向写 `workspace.json`（私有存储、version 2，升级即碎）。
   汇报给人类时给一句人话定位："在工作台侧边栏的『未分组』里，标题是 X 的那条"。

### 10.2 Windows 桌面版接内网网关：四件事（2026-11-17 实测通过）

新装一台 Windows 机器，桌面版默认只有 deepseek 官方两个模型、一问内网网关就报证书错。要让它
用上"我们的模型"，必须四层都补，少一层就是"界面能开、一问就错"：

| # | 做什么 | 判据 |
|---|---|---|
| 1 | 根证书进 Windows 证书库 **+** `NODE_EXTRA_CA_CERTS` 指向 PEM | node 探测从 `SELF_SIGNED_CERT_IN_CHAIN` 变成 `HTTP=401`；仓库现成工具 `tools/windows/install-gateway-cert.ps1`，一次装两处 |
| 2 | 渠道 provider + 默认模型写进 `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml` | 模型列表出现渠道里的模型；默认模型指向渠道 |
| 3 | 渠道密钥写进 `.credentials.yaml` 的 `refs`（名字 = provider 的 `apiKeyEnv`） | `GET <base>/v1/models` → 200，且返回的 id 与配置一致 |
| 4 | headless（干活端）的 `--patch` 覆盖层带上 `permission` presets + `defaultPreset` | 不带就报 `composed sandbox and approval defaults match no preset`；带上后 `completed` |

三条真机细节：

- 桌面端**独占管理** `profiles/desktop`：`dsh --profile desktop --dump-config` 直接被拒。
  但它**是合并而不是覆盖**——我们写进 2 条，桌面端跑过后文件变成 5 条（多了它自己的
  `ui-chat`/`ui-settings`/`ui-settings-account`），所以手写配置不会被冲掉。
- Electron 是"窗口走 Windows 证书库、Node 走 `NODE_EXTRA_CA_CERTS`"的组合；只装一处会出现
  "界面能开、一问模型就报证书错"。环境变量只对新进程生效 → **必须完全退出桌面端再打开**。
- **用户级 `NODE_EXTRA_CA_CERTS` 不一定被 GUI 继承**：实测把用户级变量写入注册表、也通知了
  Explorer 重读，**双击 exe 启动仍然报证书错**；改成"先 `set` 再启动"的 `.cmd` 启动器、
  把快捷方式指过去，才稳定可用。交付口径两条选一条：①快捷方式指向带 `set` 的启动器；
  ②把变量设成**机器级**（需要管理员）并重启 `explorer.exe`。验收判据：双击快捷方式后
  发一句模型请求能拿到回复，而不是只在 `dsh web`/CLI 里能通。

### 10.3 干活端建会话的目录纪律（2026-11-17 真机踩到）

在桌面版里点开一个**外部建的会话**想接着聊，报 `working-directory: directory does not exist`。
原因是那条会话的工作目录是临时目录（`%LOCALAPPDATA%\Temp\...`），清理临时文件时被删了——
会话记录里存的是**绝对路径**，目录没了就再也续不上，跟 app 无关。

由此定两条硬规则：

1. **干活端建会话必须用长期存在的固定工作目录**，例如
   `%USERPROFILE%\Documents\deepseek-harness\workspaces\<任务名>`；**禁止**放 `Temp`、
   `%TEMP%`、`/tmp` 或任何会被清理机制回收的路径。
2. **收尾清理不能删工作目录**：任务产物和会话记录都挂在它上面。要清理只删目录里的临时文件，
   或者把任务目录改名归档——目录本身要留着（删了等于把历史会话变成死链）。

> 复现口径：把某条会话的工作目录删掉，再在桌面版里对那条会话发消息，就会得到
> `working-directory: directory does not exist`。把目录建回来（哪怕是空目录），会话立刻恢复可续。

### 10.4 开发机验收结论（2026-11-17，全部通过）

主人现场验证三条全过：①双击快捷方式（经带证书的启动器）能开；②新会话发消息能拿到模型回复；
③在「未分组」里点开外部建的会话能接着聊。

对应的可复跑判据（同一台机器上按顺序跑）：

```
wsl -l -v                                   # VERSION 2
wsl -d <发行版> -- bash -lc 'ps -p 1 -o comm='   # systemd
dsh --version                               # 与 mac 同版本
powershell -c "$env:NODE_EXTRA_CA_CERTS"     # 指向 C:\ProgramData\dsh-certs\gateway-root.crt
curl.exe -s -o NUL -w "%{http_code}" --cacert <gateway-root.crt> https://<网关>/v1/models   # 401（未带 key）= TLS 通
```

> 结论：**Windows 开发机的「执行层 + 工作台」可以交付**。B 档后续每加一条能力，都在这台机器上
> 按"桌面版里能不能用"验收，而不是"CLI 能不能跑"。

## 11. 迁移与回滚（v10）

S3a 要给 `consumers` 加角色（`role`），并让 `events` 认 `work` 这一类；两者都落在状态库上。

- **v9 → v10**：就地迁移，一次性、幂等；迁移前自动快照（沿用 S2 的做法：
  `VACUUM INTO` 出单文件备份、`chmod 600`、`--dry-run/--verify` 两个开关）。
- `consumers.role` 默认 `'reply'`（回话端），干活端写 `'work'`；老消费者不需要改。
- **回滚两件事一起做**：恢复 `.v9-*` 备份（并删 `-wal`/`-shm`）+ `feishu-bridge rollback --to <旧 release>`。
  代码与库必须同进退：新代码打开 v9 库、旧代码打开 v10 库**都要直接报错**（下界与上界两道门禁，S1/S2 已如此）。
- **发布顺序**：停服 → 迁库 → 换 release → 验收；反了就是一次没必要的停机事故。

## 12. 剩余切片

| 切片 | 内容 | 主要文件 | 验收（可复跑） | 估时 |
|---|---|---|---|---|
| **S3a 干活端（mac）** | `work` 事件 + `consumers.role` 路由（v10）；新 `dsh-worker` 消费者；`Workbench(direct)`；任务包模板；结果回写 | `bridge/store.py`、`consumer_runtime.py`、`dsh-consumer.py`/新 `dsh-worker.py`、`install.py`、launchd 单元 | 一件真活端到端；结果是**带引用的追答**；人为制造失败也要回群；锁冲突→`blocked` | 1.5–2 天 |
| **S3b 干活端（Windows 桌面版）** | `Workbench(cmd-interop)`：路径映射、凭证 overlay、进程树收尾、锁检测 | 同上 + `bridge/platform_paths.py`（新）、systemd 单元 | 同事机跑一件真活；会话出现在桌面版列表；`MISSING_CREDENTIAL`/锁 两种失败都按设计回话 | 1–1.5 天 |
| **S4 余项** | 白名单配置化；`executor` 审计的端到端判据（引用的判定与 `approval_*` 记账已在 S2 落地） | `bridge/service.py`、`config.py` | app 身份发需求**不能**触发执行；非引用回复不认；审计能回答"谁批的、谁干的" | 0.5 天 |
| **S5 平台验收与文档** | `workbench check` 两端同一组判据；INSTALL-MACOS/INSTALL-WINDOWS 增补；`docs/HISTORY.md` 一篇 | 安装文档、`install.py`、`tools/` | mac 与 Windows 各跑一次，输出同一组 PASS 行 | 0.5–1 天 |

依赖关系：S3a → S3b；S4 余项可与 S3a 并行；S5 最后。

## 13. 风险表

| 风险 | 影响 | 缓解 |
|---|---|---|
| 干活端用错 home（两套 DSH） | 任务落进没人看得见的会话，主人以为"没做" | 配置门禁 + `workbench check` 的 `same_as_gui` 行 |
| 同一会话并发（桌面版开着 + 干活端） | 会话文件互踩、上下文错乱 | 锁检测 → `blocked`，不硬闯 |
| Windows 睡眠/WSL 挂起打断长活 | 任务半途而废且无回话 | 子进程消失检测 → `failed` + 回群说清 |
| 有人绕过桥直发飞书 | 两条声带继续打架 | 禁直发 grep 判据 + skill/文档改写 |
| 批准被冒用（别人引用回复） | 不该执行的活被执行 | open_id + 私聊 + 引用 三重判据 |
| v8 迁移出错 | 回复发不出去或重复发 | 迁移前快照 + 幂等 + 旧代码拒绝打开 |
| 两端 release 漂移 | 同一问题两个答案 | `workbench check` 带版本行；升级判据进验收 |

## 14. 开放问题（要主人拍板/需实测）

1. **哪台 Windows 是开发机？** tailnet 上有 4 台 Windows：`<机名>` 三种（一台 idle、三台 offline）。
   本次真机验证用的是台账里唯一那台同事机（她的生产机）。若要专门拿一台开发机反复折腾，
   需要指定并以台账登记。
2. **浏览器活**：ego 在 macOS 是 arm64 可执行；WSL 与 Windows 侧都没有。同事机若要做
   浏览器类任务，需要先确认 ego 有没有 Windows/WSL 版；没有就明确写"暂不支持"，不许静默。
3. **桌面版会话可见性**：外部新建的会话是否要刷新才出现在桌面版列表？需她本人看一眼确认。
4. **群里的 app 同事**（`sender_type=app`）提的活：只进草案、永不直接执行——确认这个口径。
5. **边说边做**：首答先回"收到，我去办"，追答在干完后补——确认可以接受"同一条消息两条回复"。

## 15. 附：Windows 真机验证事实快照（2026-11-16）

| 事实 | 值 |
|---|---|
| WSL 里 `dsh --version` | 0.2.1-rc.5（与 mac 同版本） |
| `dsh headless --session-id` | 可用；跨两次调用上下文连续（真机复述上一轮指令） |
| 会话落盘 | `~/.dsh/sessions/<slug>/`，slug 规则与 mac 一致 |
| 桌面版 | `DeepSeek Harness.exe`，自带 CLI `resources\runtime\cli\bin\dsh.cmd`（0.2.1-rc.5），home `C:\Users\<用户>\.dsh` |
| WSL → 桌面版 CLI | `cmd.exe /c …` interop 可调起；`DSH_HOME` 指向桌面版 home 后会话写入其 `sessions/` |
| 凭证 | 裸跑报 `MISSING_CREDENTIAL`；加三行 overlay 后成功（复用桌面版已登录的 stored 凭据） |
| `dsh web` 跨 WSL/Windows | 绑 `127.0.0.1` 时 Windows 访问不通（000）；绑 WSL IP + `--trusted-host` 后 401（要 token），带 token 后页面可加载（含 `__DSH_BOOT__`） |
| 浏览器沿 | WSL 与 Windows 均无 `ego-browser` |
| 桥 | release 落后一版（缺 `chat_type` 修复）→ 群里消息仍会被标成 p2p |
