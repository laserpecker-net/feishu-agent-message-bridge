# 飞书个人助手 · 从零安装配置指南（新手版 · 可交给 AI agent 照做）

> 这份文档写给**第一次上手、不熟悉命令行**的人。你不需要懂编程，只需要：
>
> 1. 在飞书网页后台点几下（建应用、开权限、发布版本）；
> 2. 在终端里粘贴一条命令输入密钥（这一步只有你自己能做，**不要**把 App Secret 发到聊天框里）；
> 3. 剩下的命令，**整段交给你的 AI agent（DSH / Cursor / Codex 等）执行**，它照着本文跑就行。
>
> 本文只负责"串起来 + 讲白话"。**命令与参数的权威定义在 [`CONTRACTS.md`](../CONTRACTS.md)、
> [`INSTALL-MACOS.md`](../INSTALL-MACOS.md)、[`INSTALL-WINDOWS.md`](../INSTALL-WINDOWS.md)**，
> 如果哪天本文和它们不一致，以它们为准。

---

## 0. 先用三分钟搞清楚你要装的是什么

你会得到这样一个东西：**飞书里有一个"你专属"的助手**。

- 有人在群里 @ 它 → 它读消息、查资料、在群里回话（挂在原消息下面，带引用）；
- 需要动手的事（改文件、跑命令、提交代码）→ 它先私聊发你一张卡，**等你点头**才干活；
- 你批准之后 → 由一个"干活端"在**独立目录**里干，干完把产物清单发回群里。

它由几个进程组成，各干一件事（你只会直接接触"桥"这个命令）：

| 名字 | 白话解释 | 在哪跑 |
|---|---|---|
| **桥**（`feishu-bridge`） | 收发室：把 @ 它的消息收进来排队，把回复发回去，其余一概不管 | 你的电脑，常驻 |
| **回话端**（`dsh-consumer.py`，身份名 `dsh-main`） | 大脑：读懂消息、查资料、写回答。**只读**，不会顺手改你的文件 | 你的电脑，常驻 |
| **干活端**（`dsh-worker.py`） | 手：只干你批准过的活，每件活在 `work/<任务>/space/` 里干 | 你的电脑，常驻 |
| **AI 引擎**（`dsh`） | 真正调用大模型的那个程序（没有它，桥只会收、不会答） | 你的电脑 |
| **lark-cli** | 飞书官方命令行工具，桥靠它发消息、查会话 | 你的电脑 |
| **sidecar** | 一条到飞书的长连接，专门负责"收"（只有"机器人应用"这条路线才有） | 你的电脑，常驻 |

**两条身份路线，一台电脑只能选一条：**

| 路线 | 谁在说话 | 需要什么 | 本文是否讲 |
|---|---|---|---|
| `as_user` 个人账号 | 用你自己账号的身份收发 | 用 lark-cli 登录你自己的账号 + 一堆用户权限 | 不讲，见 `INSTALL-MACOS.md` |
| `as_bot` 机器人应用（**本文**） | 一个你自建的飞书应用里的机器人 | 建一个自建应用，拿 **App ID / App Secret** | ✅ 就是这篇 |

> 为什么两条不能同时装：同一个群里会出现两个它、双份回答，而且"这句话到底是谁说的"说不清。
> 想换路线就重装一次（[§11](#11-升级回滚彻底卸载)），不要两份一起开。

**你需要准备：**

- 一台电脑：**macOS**（推荐，一条命令）或 **Windows**（必须先装 WSL2，见 [§3.4](#34-windows-用户先看这里)）；
- 你的飞书账号（自己就是管理员最好；公司租户里建应用可能需要管理员给你开发者权限）；
- 一个能调大模型的 API Key（默认用 DeepSeek；公司有内部网关也可以，见 [§3.3](#33-给-ai-准备一个大脑dsh--模型-key)）；
- 40 分钟左右的整块时间（第一次装）。

**整件事只有 3 步必须你亲手做**，其余交给 AI agent：

1. 在飞书开放平台建应用、开权限、发版本（[§2](#2-在飞书开放平台建一个机器人应用约-10-分钟)）；
2. 在终端里粘贴一次 App Secret（[§4](#4-把-client-id--secret-配进去)）；
3. 装完之后，回到后台配"事件订阅"再发一次版本（[§6](#6-配事件订阅2-分钟顺序很重要)）。

---

## 1. 先认识四个 ID / 密钥（最容易搞混的一节）

| 东西 | 长什么样 | 从哪来 | 干什么用 |
|---|---|---|---|
| **App ID**（也叫 client id / 应用 ID） | `cli_` 开头 | 飞书后台自动生成 | 告诉飞书"是哪个应用" |
| **App Secret**（也叫 client secret / 应用密钥） | 一长串随机字符 | 飞书后台自动生成，可重置 | 证明"你真是这个应用"——**等同密码** |
| **你的 open_id** | `ou_` 开头 | 用 lark-cli 查（[§4.4](#44-拿你自己的-open_id)） | 桥靠它认"主人是你" |
| **机器人的 open_id** | `ou_` 开头 | 用 lark-cli 查（[§4.3](#43-验证应用并拿到机器人的-open_id)） | 桥靠它认"群里 @ 的是它" |

**两个必须记住的事实：**

1. **open_id 是"按应用隔离"的。** 同一个人在不同应用下 open_id 不一样。所以本文要你
   "在**这个**应用下"去查你的 open_id——拿别的应用/别的工具里的 `ou_...` 来用，会表现为
   "命令不生效、它只跟你聊天"。
2. **App Secret 只允许出现在三处**（[§4.5](#45-密钥到底存在哪三处位置)），都必须是 `0600`（只有你自己能读）。
   **不要**把它写进聊天记录、截图、GitHub、笔记软件。它一旦泄露，等于别人可以用你的应用发消息。

---

## 2. 在飞书开放平台建一个机器人应用（约 10 分钟）

打开 <https://open.feishu.cn/app>（国际版 Lark 是 <https://open.larksuite.com/app>，两边的**应用数据不互通**，
按你飞书账号所在的区选一个，别混用）。

### 2.1 创建应用

1. 点 **创建应用** → 选 **企业自建应用**；
2. 填名称（例如「我的助手」）、描述、图标 → **创建**。名称以后可以改，建议别用"测试"这种名字，
   因为它在飞书里会作为机器人名字出现。

### 2.2 记下 App ID 和 App Secret

进入应用 → 左侧 **基础信息 > 凭证与基础信息**，能看到：

- **App ID**：`cli_` 开头，可以放心复制；
- **App Secret**：点"显示/复制"才看得到。**先别贴到任何聊天窗口里**，
  后面 [§4](#4-把-client-id--secret-配进去) 有一步专门让你在终端里粘贴。

> 官方说明：<https://open.feishu.cn/document/server-docs/im-v1/faq>

### 2.3 添加"机器人"能力

左侧 **添加应用能力** → 找到 **机器人** → **添加**。

没有这一步，后面的"用应用身份发消息"和"接收消息事件"都不成立。

### 2.4 开通权限（照表抄，一条都别多开）

左侧 **开发配置 > 权限管理** → **开通权限**（有的版本叫"API 权限"），在搜索框里逐条搜 **scope 名**，
勾选后确认。选择 **应用身份权限**（不是"用户身份权限"）。

分两组，都开：

**A 组：桥自己会校验的（缺了 `feishu-bridge doctor` 直接 fail）**

| # | scope（精确名） | 权限名（搜不到时按这个找） | 为什么需要 |
|---|---|---|---|
| 1 | `im:message` | 获取与发送单聊、群组消息 | 消息基础权限 |
| 2 | `im:message:send_as_bot` | 以应用的身份发消息 | 机器人回话、发卡都靠它 |
| 3 | `im:chat:readonly` | 获取群组信息 | 拉群清单、取会话信息（断线对账要用） |

**B 组：飞书后台推送事件 / 读消息正文要用（`doctor` 不校验，但缺了会"收不到消息"）**

| # | scope（精确名） | 权限名 | 为什么需要 |
|---|---|---|---|
| 4 | `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 你在私聊里给它发命令，靠这条收得到 |
| 5 | `im:message.group_at_msg:readonly` | 接收群聊中 @ 机器人消息 | 群里 @ 它，靠这条收得到 |
| 6（建议） | `im:message:readonly` | 读取消息内容 | 回话时取原消息/上下文窗口更稳 |

> 为什么分两组：A 组是**代码里的判据**（`bridge/identity/bot.py` 的 `BOT_SCOPES`，与
> INSTALL-WINDOWS.md §9 一致），缺了体检会点名；B 组是**飞书平台对"接收消息"事件的要求**，
> 桥侧查不到（读不到 scope 就不拦），缺了的表现是"长连接建起来了但一条事件都不来"。
> 权限名与 scope 的对应关系以飞书后台显示为准；后台改了措辞不影响 scope 名，按 scope 名搜最准。

### 2.5 第一次发布版本（关键：不发布 = 飞书里搜不到这个机器人）

左侧 **应用发布 > 版本管理与发布** → **创建版本**，在版本详情里：

1. **应用版本号**：填 `1.0.0`（以后每次改动要**比上一个大**）；
2. **更新说明**：随便写一句，例如"首次版本"；
3. **可用范围**：点 **编辑** → **部分成员**（或"全部成员"），**必须把你自己加进去**——
   只有可用范围内的人，才能在飞书里搜到这个机器人、才会收到它的事件；
4. **申请理由**：写一句"个人使用"；
5. 点 **保存** → **申请线上发布**。

**关于审核：** 自建应用发布需要企业管理员审核。自己一个人的租户（个人版/自己就是管理员）通常点一下就过；
公司租户里会进"管理后台 → 应用审核"或飞书审批，需要等管理员点同意（快则几分钟）。
**没通过审核 = 改动全都没生效**，这是新手最常见的"我明明配了却没用"。

> 官方说明：[发布与审核自建应用](https://open.larkenterprise.com/document/best-practices/intro-to-custom-app-review)、
> [配置应用可用范围](https://open.feishu.cn/document/develop-process/test-and-release-app/availability)

### 2.6 这一步先**不要**配事件订阅

后台的"事件与回调 → 事件配置"先放着，等 [§5](#5-安装桥一条命令) 把本机的长连接跑起来之后再回来配。
飞书官方文档明确要求**先建立长连接，再保存"使用长连接接收事件"这种订阅方式**；顺序反了，保存时
经常报"未连接/校验失败"。

### 2.7 建应用这一步的自检

- [ ] 拿到 App ID（`cli_` 开头）
- [ ] App Secret 已复制到**本机一个临时地方**（不是聊天窗口）
- [ ] "机器人"能力已添加
- [ ] 权限已开通：A 组 3 条 + B 组 3 条（见 [§2.4](#24-开通权限照表抄一条都别多开)）
- [ ] 版本已发布，且状态不是"待审核"
- [ ] 可用范围包含你自己

---

## 3. 让 AI agent 检查并准备好这台电脑

> 3.1 是**只读检查**，可以放心让 AI 跑；缺什么再按小节装（装依赖、加 PATH 会写文件，属正常安装动作）。

### 3.1 macOS 前置检查（一条一条来）

```bash
python3 --version      # 需要 3.9 或更高
node --version         # 建议 18 或更高（sidecar 长连接要用）
npm --version
git --version
lark-cli --version     # 飞书官方命令行工具
dsh --version          # AI 引擎
```

**期望**：每条都有版本号输出。**缺哪个装哪个：**

```bash
# 缺 lark-cli（飞书官方 CLI，全局 npm 包）
npm install -g @larksuite/cli

# 缺 node / npm：去 https://nodejs.org 下 LTS 版安装包，或 brew install node
# 缺 dsh（AI 引擎）
npm install -g @deepseek-ai/dsh
```

- macOS 首次运行 lark-cli 如果弹"完全磁盘访问/自动化"权限，按提示允许即可（只弹一次）。
- 桥本身**不需要 sudo**（它是用户级服务，不写系统目录）。`npm install -g` 若报权限错误，
  优先改用 nvm/官方安装包装 node；实在要 sudo 也只影响装 CLI，不影响桥。

**先修好 PATH（装完才不会"command not found"）：** 桥装好后命令是 `~/.local/bin/feishu-bridge`，
而 macOS 默认不带这个目录。先执行一次：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # bash 用户改成 ~/.bash_profile
source ~/.zshrc
command -v feishu-bridge || echo "还没装（正常）；装完应能看到路径"
```

> 不想改 PATH 也行：所有命令前面加上 `~/.local/bin/`，例如 `~/.local/bin/feishu-bridge status`。

### 3.2 取代码（已经有这个目录就跳过）

```bash
git clone https://github.com/laserpecker-net/feishu-agent-message-bridge.git
cd feishu-agent-message-bridge
```

如果你手上已经有这个仓库目录（比如 `~/Documents/feishu-personal-agent`），直接 `cd` 进去即可。
**注意：仓库不要放在 `/mnt/c`（Windows 侧盘符）、网盘、SMB 共享盘上。**

### 3.3 给 AI 准备一个"大脑"：dsh + 模型 key

回话端是一个常驻后台进程，**它不会弹窗口让你登录**，所以模型凭据要提前落到本机文件里。二选一：

**方式 A（推荐，最省事）：让它读 `~/.dsh/.env`**

```bash
mkdir -p ~/.dsh && chmod 700 ~/.dsh
printf 'DEEPSEEK_API_KEY=%s\n' 'sk-你的key' >> ~/.dsh/.env
chmod 600 ~/.dsh/.env
```

> 把 `sk-你的key` 换成你的真实 key（DeepSeek 开放平台申请的，或公司内部网关的 key）。
> 这一行你自己在终端里改，**不要**让 AI 把 key 读出来贴进聊天。

**方式 B：先用 DSH 图形界面登录一次**（登录态会存进 `~/.dsh/.credentials.yaml`，回话端会自动复用）

```bash
dsh web      # 终端会打印一个带 token 的地址，粘进浏览器
```

打开后在设置里的"模型 / 凭据"页填入 key，随便问一句能答，就说明大脑通了。关掉窗口不影响后台服务。

> 公司有内部模型网关（OpenAI 兼容接口）的机器，还要把 provider 配置写进
> `~/.dsh/feishu-consumer.patch.yml`，否则会报 `MISSING_CREDENTIAL … provider route`。
> 具体写法见 [`INSTALL-MACOS.md`](../INSTALL-MACOS.md) §6.1 最后两条。

### 3.4 Windows 用户先看这里

原生 Windows **不能**直接跑这个桥（它依赖 POSIX 的文件锁和进程组），必须先装 **WSL2**，
桥装在 WSL2 的 Ubuntu 里。这条路明显更长，权威步骤只有一份：

- 拓扑与取舍：[`WINDOWS-DECISION.md`](../WINDOWS-DECISION.md)
- 完整安装/排错：[`INSTALL-WINDOWS.md`](../INSTALL-WINDOWS.md)、[`dist/wsl2/README.md`](../dist/wsl2/README.md)

装完基础环境（一键包 `install.cmd` 跑完、`lark-cli` 已绑定应用并可用）之后，**切换成机器人身份**
要再在 WSL 里做本文 [§4](#4-把-client-id--secret-配进去) 的取 ID 步骤，然后按
[`INSTALL-WINDOWS.md`](../INSTALL-WINDOWS.md) §9（`as_bot` 一节）那条命令装——注意 Windows 上的
bridge/消费者由系统级 unit 托管，**别只手工跑其中一半**。装完应有的两个额外单元是
`io.feishu-agent.identity-sidecar`（长连接）和 `io.feishu-agent.ingest-gateway`（本机回环事件网关），
都用 `systemctl --user is-active <unit>` 检查，再用 `feishu-bridge doctor --assert` 确认 rc=0。

---

## 4. 把 client id / secret 配进去

这一步有个反直觉的地方：**同一个 App Secret 要配两遍**，因为收和发是两条不同的通道。

| 配给谁 | 干什么 | 存到哪 |
|---|---|---|
| `lark-cli` | 发消息、查会话（桥的"发"） | `~/.lark-cli/config.json`（0600，可能还有系统钥匙串） |
| 桥的 sidecar | 收消息（桥的"收"） | `~/.feishu-app-secret`（0600），安装器再把它拷进 `<桥目录>/identity-sidecar.env`（0600） |

### 4.1 告诉 AI 你的 App ID（不是 Secret）

App ID 不是密码，可以直接发给 AI。例如：

> 我的飞书 App ID 是 `cli_你的应用ID`，品牌是飞书（中国版）。请按 docs/GUIDE-BEGINNER.md 继续。

### 4.2 输入 App Secret（**这一条请你自己在终端里跑**）

在终端里粘贴下面整段，回车后**把 App Secret 粘到提示后面再回车**（不会回显、不进 shell 历史）。
这段写法在 macOS 默认的 zsh 和 bash 里都能跑：

```bash
printf '把 App Secret 粘到这里后按回车（不会显示）: '
read -r -s FEISHU_APP_SECRET; echo
printf '%s' "$FEISHU_APP_SECRET" | lark-cli config init --app-id 'cli_你的应用ID' --brand feishu --app-secret-stdin
umask 077 && printf '%s' "$FEISHU_APP_SECRET" > "$HOME/.feishu-app-secret"
unset FEISHU_APP_SECRET
lark-cli doctor      # 期望 ok:true，且 app_resolved / bot_identity 是 pass
```

要点：

- `--brand feishu` 是**飞书中国版**；Lark 国际版改成 `--brand lark`。选错的表现是所有接口 4xx。
- 如果这台机器以前配过别的应用，`lark-cli config init` 可能提示已有配置；按提示确认覆盖即可
  （一台机器只用一套应用配置，别同时留两个）。
- 跑完 `~/.lark-cli/config.json` 和 `~/.feishu-app-secret` 都应该是 `0600`：

```bash
ls -l ~/.lark-cli/config.json ~/.feishu-app-secret    # 期望 -rw------- （0600）
```

### 4.3 验证应用并拿到机器人的 open_id

```bash
lark-cli doctor                                           # 期望 ok:true，且 bot_identity 是 pass/ready
lark-cli api GET /open-apis/bot/v3/info --as bot          # 期望 ok:true
```

第二条返回的 `data.open_id`（`ou_` 开头）**就是机器人自己的 open_id**，记下来，[§5](#5-安装桥一条命令) 要用。

如果这里报 `authorization` / `app not found` / 4xx：

- 检查 App ID、App Secret 是否抄错、是不是同一个应用的；
- 检查 `--brand` 选对没有；
- 检查 [§2.5](#25-第一次发布版本关键不发布--飞书里搜不到这个机器人) 的版本是否真的发布通过了。

### 4.4 拿你自己的 open_id

需要**你这个人在这个应用下的 open_id**。最直接的办法是用这个应用做一次用户授权（只为拿 ID，日常不靠它）：

```bash
lark-cli auth login --scope "im:chat:read"   # 按提示在浏览器里同意（会自己打开或打印一个链接）
lark-cli auth status --json                  # 看 identities.user.openId，ou_ 开头就是
```

- 如果 `auth login` 报某个 scope 未开通：去后台 [§2.4](#24-开通权限照表抄一条都别多开) 把
  `im:chat:read` 作为**用户身份权限**开通 → 重新发布版本 → 再 `auth login` 一次；
  或者去掉 `--scope`，让它按默认范围授权（范围更大，能拿到 ID 就行）。
  **这一步只是为了取 ID，不影响桥的使用。**
- 取到之后可以 `lark-cli auth logout`（可选，减少本机多一份用户 token；留着也不影响桥）。

### 4.5 密钥到底存在哪三处位置

| 位置 | 内容 | 谁读它 | 权限 |
|---|---|---|---|
| `~/.lark-cli/config.json` | App ID + App Secret + brand（macOS 上 secret 也可能在系统钥匙串） | `lark-cli`（发消息） | 0600 |
| `~/.feishu-app-secret` | 只有 App Secret 一行 | 安装器（每次安装/升级时读） | 0600 |
| `<桥目录>/identity-sidecar.env` | App ID + App Secret + 回环地址 + token 路径 | sidecar 长连接进程 | 0600 |

`<桥目录>`（bridge home）在 macOS 是 `~/Library/Application Support/FeishuPersonalAgent`。
桥自己的 `config.json` 里**只存 app_id 和密钥文件的路径，不存明文 secret**——这是故意的。

---

## 5. 安装桥（一条命令）

### 5.1 先把 AI 的工作目录建出来

```bash
mkdir -p "$HOME/dsh-workspace"
```

这就是 AI 的"工作空间"：回话端在这个目录里读文件、回答问题时以它为根（**只读**，它改不了这里）。
想让它能看你的某个项目，就把这个路径换成那个项目目录，见 [§7](#7-指定工作空间三个目录别弄混)。

### 5.2 安装命令

把命令里所有占位符换成你自己的（`cli_` 应用 ID、两个 `ou_` 来自 [§4.3](#43-验证应用并拿到机器人的-open_id)、
[§4.4](#44-拿你自己的-open_id)），`cd` 那行换成你的仓库真实路径，然后整段交给 AI：

```bash
cd ~/Documents/feishu-personal-agent     # ← 换成你的仓库真实路径

TARGET_OPEN_ID='ou_你本人的' \
python3 install.py \
  --identity-mode bot \
  --bot-app-id 'cli_你的应用ID' \
  --bot-open-id 'ou_机器人的' \
  --owner-open-id 'ou_你本人的' \
  --bot-app-secret-file "$HOME/.feishu-app-secret" \
  --with-dsh-consumer \
  --dsh-workspace "$HOME/dsh-workspace" \
  --signature '此回复信息来自我的 AI 助手' \
  --verify
```

每个参数在说什么：

| 参数 | 含义 |
|---|---|
| `TARGET_OPEN_ID=...` | 这台机器的主人是谁（**必须是你在这个应用下的 open_id**，`ou_` 开头） |
| `--identity-mode bot` | 用"机器人应用"身份（一台机器只能一种身份，换成 `user` 就是个人账号路线） |
| `--bot-app-id` / `--bot-app-secret-file` | 收消息的 sidecar 用哪个应用、密钥从哪读（文件必须存在，建议 0600） |
| `--bot-open-id` | 机器人自己的 open_id（强烈建议给；不给会“@ 判定放宽”，可能多处理消息） |
| `--owner-open-id` | 谁能批准卡、谁能发命令（可以写多次，多个人就用多个 `--owner-open-id`） |
| `--with-dsh-consumer` | 顺带装"回话端 + 干活端"两个常驻进程（**不装 = 只收不回**） |
| `--dsh-workspace` | AI 的工作空间（绝对路径，[§7](#7-指定工作空间三个目录别弄混)） |
| `--signature` | 回复末尾的署名（不超过 60 字）。可以写 `{owner}` 占位符，它在**个人账号身份**下会渲染成飞书账号姓名；**机器人身份下没有"本人姓名"可取，会回落成中性词「账号本人」**，所以这条路线建议直接写固定文案 |
| `--verify` | 装完立刻再跑一次体检（有 fail 时退出码 1） |

### 5.3 装完会看到什么

**退出码是唯一的判据**：

| 退出码 | 含义 | 下一步 |
|---|---|---|
| `0` | 装好并已自动启动（心跳已是新版本） | 继续 [§5.4](#54-装完立刻做两件事) |
| `1` | 失败。**两种时机**：① 前置检查/构建阶段失败（还没写盘）；② 写盘之后才失败（缺 `node`/`npm`、密钥文件不存在、`--verify` 体检有 fail） | 看 stderr 原文与回执里的 `activation`：前者修好后重跑；后者**先别慌，重跑同一条命令**，报错没变再按提示补 |
| `3` | 代码已提交，但进程没跑起来 | 按 stderr 里的日志路径查；最常见是"AI 大脑没配好"（[§3.3](#33-给-ai-准备一个大脑dsh--模型-key)） |

装完后，你机器上会多出这些常驻服务（macOS 用户级 LaunchAgent，**不需要 sudo**）：

| 服务 | 作用 |
|---|---|
| `io.feishu-agent.bridge` | 桥本体 |
| `io.feishu-agent.dsh-consumer` | 回话端（`dsh-main`） |
| `io.feishu-agent.dsh-worker` | 干活端 |
| `io.feishu-agent.identity-sidecar` | 长连接收事件（bot 身份专有） |
| `io.feishu-agent.ingest-gateway` | 只绑 `127.0.0.1:8787` 的本机事件网关，sidecar 把事件交给它 |

### 5.4 装完立刻做两件事

**第一件：放行发送。** 新装默认 `sending_enabled=false`——这时桥会照常收消息，但**消费者不领事件**，
所以没人回话（这不是坏了，是防止刚装好就乱回话）：

```bash
feishu-bridge resume
feishu-bridge status | grep -E 'sending_enabled|authenticated'
```

期望：`sending_enabled` 变成 `true`、`authenticated` 是 `true`。

**第二件：体检。**

```bash
feishu-bridge doctor --assert; echo "rc=$?"
```

期望 `rc=0`，且列表里这几行是 `ok`：

- `authenticated`（bot 身份就绪、app_id 与配置一致）
- `identity` / `identity_channel`（`mode=bot`、`ws(sidecar=service → /v1/ingest)`）
- `ingest_token` / `ingest_gateway`（回环网关在听）
- `identity_sidecar`（长连接单元真的起来了）
- `workers_complete`（桥的几个后台线程都活着）

此时**还没配事件订阅**，所以"能收消息"这件事还不成立——听不听得见飞书的消息，是下一步。

---

## 6. 配事件订阅（2 分钟，顺序很重要）

现在本机的长连接已经在跑了，回到飞书后台：

1. 左侧 **开发配置 > 事件与回调** → **事件配置**；
2. **订阅方式** 那一行点 **编辑** → 选 **使用长连接接收事件** → **保存**；
   - 如果保存时报"未连接/校验失败"：说明本机 sidecar 没跑起来，回 [§5.4](#54-装完立刻做两件事) 查
     `identity_sidecar` 和日志，别在后台反复点；
3. **已添加事件** 区域点 **添加事件** → 搜"接收消息"（或直接搜 `im.message.receive_v1`）→ 添加；
4. 左侧 **应用发布 > 版本管理与发布** → **创建版本** → 版本号改成 `1.0.1` → **申请线上发布**。
   这是飞书平台的规定：改了权限/事件订阅/可用范围，都必须发新版本并通过审核才生效。

> 官方说明：[使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)、
> [应用配置说明（含事件与权限清单）](https://open.feishu.cn/document/uAjLw4CM/uMzNwEjLzcDMx4yM3ATM/develop-an-echo-bot/faq)

**怎么确认真的收到了？** 看 sidecar 日志里出现 `forwarded om_...`（`om_` 是飞书消息 ID）：

```bash
tail -n 30 "$HOME/Library/Application Support/FeishuPersonalAgent/identity-sidecar.stdout.log"
```

只有 `ws client ready` **不算**收到——那只证明连接建立了，不证明事件来了。

---

## 7. 指定工作空间（三个目录，别弄混）

| 目录 | 默认值 | 是什么 | 怎么改 | 谁写 |
|---|---|---|---|---|
| **桥目录**（bridge home） | macOS：`~/Library/Application Support/FeishuPersonalAgent` | 桥的数据库、日志、配置、任务目录 | 安装时 `FEISHU_BRIDGE_HOME=/your/path` | 桥自己 |
| **AI 工作空间** | `~/dsh-workspace` | 回话端"看得见"的根目录（**只读沙箱**：能读，不能写） | 重跑安装命令，改 `--dsh-workspace` | 不写（写请求会被沙箱拒绝） |
| **干活端目录根** | `<桥目录>/work` | 每件批准过的活在这里开一个 `<work_root>/<任务>/space/` | 改 `config.json` 的 `worker.work_root`，再**重跑安装命令**（它会重载所有服务） | 干活端 |

**改 AI 工作空间（让它能读你的项目）**：重跑同一条安装命令，只把 `--dsh-workspace` 换成你的项目绝对路径：

```bash
TARGET_OPEN_ID='ou_你本人的' \
python3 install.py --identity-mode bot \
  --bot-app-id 'cli_你的应用ID' --bot-open-id 'ou_机器人的' \
  --owner-open-id 'ou_你本人的' \
  --bot-app-secret-file "$HOME/.feishu-app-secret" \
  --with-dsh-consumer \
  --dsh-workspace "$HOME/Documents/我的项目" \
  --verify
```

> 同样的参数重跑 = **升级**（会切新版本并重载服务），配置不会丢。**以后每次升级都用同一条命令。**

**改干活端目录根**（改完**必须重跑 [§5.2](#52-安装命令) 的安装命令**——干活端是独立进程，
启动时只读一次配置，单独 `feishu-bridge restart` 不会让它重读）：

```bash
python3 - <<'PYEOF'
import json, pathlib
home = pathlib.Path.home() / 'Library/Application Support/FeishuPersonalAgent'
path = home / 'config.json'
config = json.loads(path.read_text())
config.setdefault('worker', {})['work_root'] = str(pathlib.Path.home() / 'feishu-work')
path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
print('work_root =', config['worker']['work_root'])
PYEOF
mkdir -p "$HOME/feishu-work"
# 然后重跑 §5.2 的安装命令（同参数，不用改别的）→ 桥 / 回话端 / 干活端一起重载
feishu-bridge doctor --assert; echo "rc=$?"
```

**三条经验：**

1. **不要**把桥目录或干活目录放到网盘 / SMB / iCloud Drive：数据库是 SQLite（WAL），网络盘上多写者会坏。
2. Windows（WSL2）上干活端目录要指到 `/mnt/c/...`（即 Windows 的 `C:\...`），否则你在 Windows 里看不到产物。
3. 工作空间**必须是绝对路径**（相对路径会被安装器直接拒绝）；目录不存在没关系，安装器会 `mkdir -p` 建出来。

---

## 8. 怎么用（日常）

### 8.1 第一次：私聊它

飞书里搜你给应用起的名字（机器人名字默认就是应用名，也可以在后台「应用能力 > 机器人」单独改）
→ 点开私聊 → 发一个字不差的：

```
help
```

它会回一份完整使用说明（长文会切成几条，末尾带"（完）"）。**这是判断"整条链路通了"最省事的办法。**

如果没回：先 `feishu-bridge status` 看 `authenticated` / `sending_enabled`，
再 `feishu-bridge consumer status` 看 `dsh-main` 是否 `live`，然后看 [§10](#10-出问题怎么查新手最常见的十个坑)。

### 8.2 群里用

1. 把机器人**拉进群**（或者在建群时添加机器人）；
2. 在群里 **@它** + 说清要求，它就会回话（挂在原消息下面，带引用）。

群里没 @ 它的话不会处理。**群里的命令一律不执行**（命令只认你私聊它）。

### 8.3 命令表（必须一个字不差）

| 命令 | 什么时候用 | 示例 |
|---|---|---|
| `help` | 忘了某个命令怎么用 | `help` |
| `状态` | 不确定它还在不在跑、有几张卡等你批（会编号） | `状态` |
| `执行` | 批准唯一一张待批的卡 | `执行` / `执行 2`（批第 2 张） |
| `上班` | 之前发过 `下班`，现在回来 | `上班` |
| `下班` | 去开会/下班，不想它在这期间插话 | `下班` |
| `记住：` | 让它记住一条规矩（冒号后面接内容） | `记住：汇报先给结论` |
| `清单` | 看它现在记住了哪些规矩 | `清单` |
| `撤回` | 撤回最近记住的一条（历史留档不删） | `撤回` |

> 注意：`"停一下"`、`"继续"`、`"帮助"` 这类**口语不算命令**，它会回一句"命令要精确"。
> 命令与聊天的边界是硬规则（确定性字符串匹配），不靠 AI 猜。

### 8.4 批卡（需要动手的事）

它要动东西时，会私聊发你一张卡，你可以：

- 回 `做` —— 开始干；
- 回 `不做` —— 作罢；
- 回 `做，但只做①` —— 只做第一步；
- 或直接回 `执行`（只有一张卡时）/ `执行 2`（多张时按 `状态` 里的编号）。

**同时等你的卡不止一张时**，要用"引用那条卡再回"的方式，不然它分不清你要批哪张。
**卡有 30 分钟有效期**，过期作废（下班前别把卡批到一半）。

### 8.5 它会做什么、不会做什么

- 会：读消息、查资料（本机文件只读 + 联网）、代你起草回复；批准后在独立目录里执行任务、交产物清单。
- 不会：替你点头、在群里乱发、删除/破坏性操作、读取或外传密钥密码、委派子代理/用 MCP。
- 每条回复都带署名（[§5.2](#52-安装命令) 的 `--signature`），能区分"人说的"和"助手说的"。

### 8.6 终端里常用的几条

```bash
feishu-bridge status                 # 在不在跑、身份、发送开关、积压
feishu-bridge doctor --assert        # 体检（有 fail 退出码 1）
feishu-bridge consumer status        # 谁在领事件（dsh-main / dsh-worker 是否 live）
feishu-bridge logs --lines 100       # 最近的业务日志（bridge.log）
tail -f "$HOME/Library/Application Support/FeishuPersonalAgent/bridge.log"   # 跟踪（Ctrl-C 退出）
feishu-bridge logs --all --lines 200 # 连各服务的 stdout/stderr 一起看
feishu-bridge hold -r "下班"          # 命令行挂起（等价于发「下班」）
feishu-bridge release                # 命令行恢复
feishu-bridge commands               # 打印命令表
```

---

## 9. 验收清单（可复跑）

装完 / 改完 / 升级完，按这个顺序过一遍，**全绿才算完**：

**终端（5 条）**

- [ ] `feishu-bridge status` → `process_running=true`、`authenticated=true`、
      `running_release == installed_release`、`update_pending=false`、`sending_enabled=true`
- [ ] `feishu-bridge doctor --assert; echo rc=$?` → `rc=0`
- [ ] `feishu-bridge consumer status` → `dsh-main` 和 `dsh-worker` 都是 `live`
- [ ] `tail -n 50 "<桥目录>/identity-sidecar.stdout.log"` → 能看到 `websocket long connection started`
- [ ] `ls -l ~/.feishu-app-secret ~/.lark-cli/config.json` → 都是 `0600`

**飞书里（3 条）**

- [ ] 私聊发 `help` → 收到完整说明书（切成几条）
- [ ] 群里 `@它 你好` → 收到一条回复，挂在原消息下面（引用正确）
- [ ] 私聊发 `状态` → 收到状态回执

**再加一条最硬的判据**（可选，验证"收得准"）：在群里 @ 它问一句只有你电脑上有的信息
（比如"我桌面上有几个文件"），它答得出来 → 说明回话端、沙箱、工作空间都通了。
需要动手的活（例如"把 A 文件里的数字改成 2"）会先给你发卡，批了才动。

---

## 10. 出问题怎么查（新手最常见的十个坑）

| 现象 | 最可能的原因 | 怎么修 / 怎么验证 |
|---|---|---|
| 飞书里**搜不到**机器人 | 应用没发布，或可用范围没有你 | 回后台看版本状态、把可用范围加上你 → 重新发布 |
| 私聊发 `help` **没有任何回复** | sidecar 没跑 / 事件订阅没保存 / 发送被关 | `feishu-bridge doctor --assert` 看 `identity_sidecar`、`ingest_gateway`；`feishu-bridge resume`；回 [§6](#6-配事件订阅2-分钟顺序很重要) |
| 日志里有 `ws ... started`，但就是收不到事件 | **长连接被别的客户端抢了**（一个应用只能有一个长连接客户端） | 关掉同应用的其它长连接程序（别的电脑/别的工具里跑的这个应用），再 `feishu-bridge restart`；判据是日志出现 `forwarded om_...` |
| `status` 里 `authenticated=false`（bot） | app_id 与 lark-cli 里配置的不一致 / token 过期 / 密钥文件不对 | `lark-cli doctor`、`lark-cli api GET /open-apis/bot/v3/info --as bot`；改完重跑安装命令 |
| 回话端起不来（`consumer status` 里不 live） | AI 大脑没配好（没 key / key 无效） | 看 `<桥目录>/dsh-consumer.stderr.log`（安装器给每个服务写一对 `<名字>.stdout.log` / `.stderr.log`）；按 [§3.3](#33-给-ai-准备一个大脑dsh--模型-key) 配 key，然后 `install.py ... --verify` 重跑 |
| 它能聊天，但发命令（`状态`/`上班`）没反应 | 命令发在了**群里**，或发命令的人不在 `owner_open_ids` 里，或 open_id 是别的应用下的 | 命令只在**你与机器人的私聊**里生效；确认 `--owner-open-id` 用的是"这个应用下"的 open_id |
| 它答上来了，但飞书里看不到回复 | 发送开关被关（新装默认关）/ 发送失败 | `feishu-bridge status` 看 `sending_enabled`；`feishu-bridge resume`；`feishu-bridge logs --all --lines 200` |
| 群里别人 @ 它，它也回 | 这是设计如此（它服务的是"被 @"，不是"只回你") | 不想这样就把机器人从那个群移除；命令通道仍然只有你能用 |
| 装完改了参数，行为没变化 | 只重载了桥，没换到新配置 / 参数没重跑 | 用**同样的命令（带上新参数）**重跑 `install.py`，它会把服务重新加载；再 `doctor --assert` |
| `update_pending=true` | 装过新版本但进程还跑旧版 | `feishu-bridge restart`；再看 `status` |

**排查顺序（照这个来，不要乱试）**：`feishu-bridge status` → `doctor --assert` → `consumer status`
→ `feishu-bridge logs --all --lines 200`（它会把桥目录下所有 `*.log` 一起打出来，含 `identity-sidecar.*.log`）
→ 最后才回头看飞书后台配置。
**报错原文要保留**，不要只看"失败了"三个字。

---

## 11. 升级、回滚、彻底卸载

### 11.1 升级（包括"改参数"）

用**同一条安装命令**重跑一遍（参数按需改），它会构建新版本、切换、重载所有服务：

```bash
cd ~/Documents/feishu-personal-agent
git pull --ff-only          # 从仓库更新代码（你自己的副本，没有远端就跳过）
# 然后重跑 [§5.2](#52-安装命令) 那条命令（把 --dsh-workspace 等参数保持你想要的）
```

关键点：**升级 = 重跑安装命令**；不要靠手工 `launchctl` 或改 plist 来换版本（会留下"plist 是新的、进程跑旧的"）。装完用 [§9](#9-验收清单可复跑) 再验一遍。

### 11.2 回滚到上一个版本

```bash
feishu-bridge rollback --list                 # 看有哪些已安装版本
feishu-bridge rollback --to <release_id>      # 切回去
feishu-bridge restart                         # 桥：重启进程才真的换代码
# 两个消费者也要重启，否则它们还跑着启动时加载的那份代码
launchctl kickstart -k "gui/$(id -u)/io.feishu-agent.dsh-consumer"
launchctl kickstart -k "gui/$(id -u)/io.feishu-agent.dsh-worker"
sleep 5 && feishu-bridge consumer status      # 期望 dsh-main / dsh-worker 都 live
feishu-bridge doctor --assert; echo rc=$?
```

> 回滚的边界：升级会**就地迁移** `state.sqlite3` 且没有 down 脚本，所以旧 release 必须能读当前库的
> schema；跨大版本回滚要连数据库一起退回升级前的快照（见 `docs/ops-remote-manage.md`）。
> 刚升完想退回上一版不受影响。

### 11.3 暂时不用它

```bash
feishu-bridge hold -r "下班"    # 零采集、零回复（重启后保持）
feishu-bridge release           # 恢复
```

想停进程但保留数据：

```bash
feishu-bridge stop
# 恢复：
feishu-bridge start
```

### 11.4 切换身份（bot ↔ 个人账号）

**必须先手工删掉 bot 配置块**，否则安装器会拒绝——它把"user 模式 + 残留 `identity.bot.app_id`"
判为配置矛盾：

```bash
python3 - <<'PYEOF'
import json, pathlib
path = pathlib.Path.home() / 'Library/Application Support/FeishuPersonalAgent/config.json'
config = json.loads(path.read_text())
block = config.get('identity') or {}
block.pop('bot', None)          # 只删 bot 块，保留其余键
block['mode'] = 'user'
config['identity'] = block
path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
print('identity =', json.dumps(block, ensure_ascii=False))
PYEOF

# 然后重跑安装（它会停用并删除 sidecar 与 ingest-gateway）
python3 install.py --identity-mode user
```

> 切回个人账号路线还要另外配 lark-cli 登录与用户权限（见 `INSTALL-MACOS.md`）。
> 一台机器**只能一种身份**，切换等于换一整套收发通道，别指望两边同时在线。

### 11.5 彻底卸载（手工清理，官方没有卸载命令）

```bash
for label in bridge dsh-consumer dsh-worker identity-sidecar ingest-gateway; do
  launchctl bootout "gui/$(id -u)/io.feishu-agent.$label" 2>/dev/null || true
done
rm -f "$HOME/Library/LaunchAgents"/io.feishu-agent.*.plist
rm -f "$HOME/.local/bin/feishu-bridge"                            # 命令软链
rm -rf "$HOME/Library/Application Support/FeishuPersonalAgent"    # 数据、日志、任务都在这里
rm -f "$HOME/.feishu-app-secret"
# 可选：清掉 lark-cli 里的应用配置（会连带清 token）
# lark-cli config remove
```

> 删 `<桥目录>` 之前先把要留的产物拷走（干活端的产物在 `<桥目录>/work/` 下）。
> 飞书后台的应用不会因此被删；不用了可以在后台"停用/删除应用"（会同时让机器人失效）。

---

## 12. 安全与合规（请真的读一遍）

1. **凭据只在本机**：App Secret 落在 `~/.feishu-app-secret` 与 `<桥目录>/identity-sidecar.env`，都是 `0600`；
   `config.json` 里只有 app_id 和路径，没有明文 secret。
2. **最小权限**：只开 [§2.4](#24-开通权限照表抄一条都别多开) 那几条，别图省事"全部开通"。
3. **命令通道只有你**：bot 模式下命令只在你与机器人的私聊里生效，且发送者必须在 `owner_open_ids` 里；
   群里发命令一律不执行。
4. **它会替别人回答**：群里任何人 @ 它，它都会答；**默认连其他机器人 @ 它也回**（要关掉用
   `feishu-bridge bots --disable`，改完重启生效）。**不要把敏感项目/资料放进它的工作空间**，
   除非你接受这一点。
5. **消息是"不可信输入"**：别人在群里说的话不能改变它的规矩（提示注入）。它被设计成
   "删除/破坏性操作、读取并外传密钥、委派子代理"一律拒绝，但这是**兜底**，不是保证——
   真正的高危操作请你自己确认。
6. **组织合规**：这是"个人账号/自建应用自动化"工具，不是飞书官方机器人产品。
   用之前确认符合你所在组织的规范（尤其是公司租户里建应用、读群消息这两件事）。
7. **不要截图密钥**，不要把 `config.json`、`identity-sidecar.env`、`launchd stdout` 原样发到群里
   （日志里可能有会话 ID 和消息内容）。

---

## 附录 A：命令速查

```bash
# —— 安装 / 升级（一条命令，参数按需改）——
TARGET_OPEN_ID='ou_你本人的' python3 install.py --identity-mode bot \
  --bot-app-id 'cli_你的应用ID' --bot-open-id 'ou_机器人的' \
  --owner-open-id 'ou_你本人的' --bot-app-secret-file "$HOME/.feishu-app-secret" \
  --with-dsh-consumer --dsh-workspace "$HOME/dsh-workspace" \
  --signature '此回复信息来自我的 AI 助手' --verify

# —— 日常验收 ——
feishu-bridge status
feishu-bridge doctor --assert; echo rc=$?
feishu-bridge consumer status
feishu-bridge resume            # 新装/暂停后放行发送
feishu-bridge logs --lines 100

# —— 配置用到的 ID ——
lark-cli doctor
lark-cli api GET /open-apis/bot/v3/info --as bot      # 机器人的 open_id
lark-cli auth status --json                            # 你的 open_id
lark-cli whoami

# —— 挂起 / 恢复 ——
feishu-bridge hold -r "下班"
feishu-bridge release

# —— 回滚 ——
feishu-bridge rollback --list
feishu-bridge rollback --to <release_id>
feishu-bridge restart
```

## 附录 B：文件位置一览（macOS）

| 路径 | 是什么 |
|---|---|
| `~/Library/Application Support/FeishuPersonalAgent/` | 桥目录：配置、数据库、日志、任务产物、release 快照 |
| `…/config.json` | 唯一配置（`target_open_id` / `signature` / `sending_enabled` / `identity` / `worker`） |
| `…/identity-sidecar.env` | sidecar 的环境（含 App Secret，0600） |
| `…/gateway-token` | 本机回环网关的共享 token（0600） |
| `…/state.sqlite3` | 事件/任务队列与账本（SQLite WAL，别放网络盘） |
| `…/bridge.log`（+ 轮转的 `bridge.log.1`、`.2`…） | 业务日志（`feishu-bridge logs` 默认看它） |
| `…/<服务名>.stdout.log` / `.stderr.log` | 每个服务的系统层日志：`launchd.*`（桥）、`dsh-consumer.*`、`dsh-worker.*`、`identity-sidecar.*`、`ingest-gateway.*`（`feishu-bridge logs --all` 一起看） |
| `…/work/<任务>/space/` | 干活端的任务目录（产物在这里）。默认 `work_root` 是 `<桥目录>/work`，改过就以你配的为准 |
| `…/releases/<id>/` + `…/current` | 不可变版本快照 + 唯一切换点（升/降级只动 `current`） |
| `~/Library/LaunchAgents/io.feishu-agent.*.plist` | 用户级服务定义（bot 路线全新安装 5 个；装过 agentkit 的机器会多一个 `agentkit-consumer`） |
| `~/.feishu-app-secret` | 只放 App Secret 一行（0600） |
| `~/.lark-cli/config.json` | lark-cli 的应用配置（App ID/Secret/brand，0600） |
| `~/.dsh/.env`、`~/.dsh/.credentials.yaml` | AI 引擎的模型凭据 |

## 附录 C：给 AI agent 的执行约定

如果你是把这份文档交给 AI 来执行，请让它遵守：

1. **绝不回显 App Secret**：不打印、不写进聊天、不写进任何日志或临时文件；只通过 `read -s` 由人输入一次。
2. **一次只跑一段**：每段命令跑完贴出**真实输出**，不要"看起来成功"就往下走。
3. **安装前先看现状**：`feishu-bridge status`（已装过别的身份/别的应用就先说清，别直接覆盖）。
4. **失败不要连试三次**：留住报错原文，按 [§10](#10-出问题怎么查新手最常见的十个坑) 的排查顺序走。
5. **不要 sudo**：所有服务都是用户级；不要改系统目录、不要改 `/etc`。
6. **装/升级不要手工改 plist**：一律重跑 `install.py`（它负责切版本 + 重载）。手工 `launchctl` 只在两处：
   [§11.2](#112-回滚到上一个版本) 回滚后重启两个消费者、[§11.5](#115-彻底卸载手工清理官方没有卸载命令) 卸载。
7. **不要动 `<桥目录>/state.sqlite3`、`releases/`、`current`**；排障只看、不改。
8. **验收要有判据**：每步给出"期望输出 / 退出码"，并以 [§9](#9-验收清单可复跑) 的清单收尾。
9. **密钥与日志不外传**：不把 `identity-sidecar.env`、`config.json`、日志原文发到聊天或第三方服务。
10. **拿不准就问人**：尤其是"要不要换身份""要不要删数据""要不要重装"这三类操作。
