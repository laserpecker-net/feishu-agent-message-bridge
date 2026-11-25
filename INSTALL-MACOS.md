# macOS 安装与使用指南（按 Agent 分）

适用：**bridge 本体只装在 Mac**；Mac 上可同时接 DSH / Codex / Cursor / 自研 Agent 作为消费者。
协议契约见 [`CONTRACTS.md`](CONTRACTS.md)。

当前生产：以 `feishu-bridge status` 的 `running_release` 为准（本文不写死 release 号），`agentkit-cursor` 常驻消费 message 事件。**bridge 自己没有内置大脑**：收到的 @ 写成 `message` 事件，等消费者 claim 并提交回复。
源码目录：`~/Documents/feishu-personal-agent`。

---

## 0. 角色与选择：先决定谁回复

| 方式 | 谁在跑 | 适用 Agent | 入口 |
|---|---|---|---|
| **会话式**（skill + `/loop`） | DSH / Codex 的一个专用会话 | DSH、Codex | `skills/feishu-bridge-consumer/SKILL.md` + `SESSION-PROMPT.txt` |
| **常驻进程式**（daemon） | 独立 LaunchAgent/进程，自带生成适配器 | Cursor（生产）、DSH、Codex、自研 | `agentkit-consumer.py`、`dsh-consumer.py`、`codex-consumer.py`、`feishu-bridge consumer status` |
| **远程式**（HTTP 网关） | 消费者在另一台机器 | 跨机消费者 | `skills/feishu-bridge-consumer-remote/SKILL.md` |

**关键规则**：所有已注册消费者**竞争**同一条 message 事件，bridge 不按 Agent 名称分流。要指定某个 Agent 独占，必须停掉其它 message 消费者（见 §7）。

---

## 1. 前置条件（bridge 主机）

- macOS，Python **3.9+**（`python3 --version`）
- `lark-cli` 已安装并完成个人号登录（bridge 用个人身份收发）
- 当前用户能写 `~/Library/Application Support/FeishuPersonalAgent` 与 `~/Library/LaunchAgents`
- 首次运行 lark-cli 若弹系统权限（完全磁盘访问/自动化），按提示授权

```bash
lark-cli auth status            # 确认已登录（命令以本机 lark-cli 为准）
python3 --version
```

---

## 2. 安装 / 升级 / 回滚 bridge

**首次安装要先拿到你自己的 open_id**（`install.py` 全平台统一要求，**没有默认值**）：

```bash
# ① 登录（已登录可跳过）
lark-cli auth login
lark-cli auth status              # 期望 tokenStatus: valid / available: true

# ② 取你自己的 open_id（= identities.user.openId，ou_ 开头）
lark-cli auth status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["identities"]["user"]["openId"])'
```

- 这个值必须**等于你登录的飞书用户**：bridge 每 60s 比对一次 `identities.user.openId` 与 `config.json` 的 `target_open_id`，不等就 `authenticated=false`、不回复。
- 必须是 `ou_` 开头，否则 `install.py` 直接拒绝。
- **首次安装必须传；升级/重装可以省略**（沿用 `~/Library/Application Support/FeishuPersonalAgent/config.json` 里已有的 `target_open_id`）。旧安装里如果存的正是仓库作者的历史默认值，只会打一条 `warning`，不阻断。

```bash
cd ~/Documents/feishu-personal-agent
python3 -m pytest tests/ -q   # 全绿；沙箱/受限环境里 2 个 process-group 用例因 ps EPERM 失败，属环境限制
TARGET_OPEN_ID=ou_你自己的id python3 install.py   # 首次安装必填；升级/重装可省略该前缀
~/.local/bin/feishu-bridge status                 # 装完即在跑：脚本已激活并等到心跳
feishu-bridge consumer status                     # 消费者是否在领事件（dsh-main live=true）
"$HOME/Library/Application Support/FeishuPersonalAgent/current/agentkit-consumerctl.py" restart  # 只有装了 agentkit 入口时才需要（它不跟 current）
```

**安装不含"大脑"**：`install.py` 只装桥。不加 `--with-dsh-consumer`（也没接别的消费者）时，它会打一条显眼的 `warning`——桥照常收消息、写事件，但没人回答，`doctor --assert` 的 `consumer_offline` 会 fail。macOS 上一台机器要有大脑，就用 `python3 install.py --with-dsh-consumer`（见本节「常驻消费者」）。

**回复署名**（与 Windows 安装器同一套语义）：`install.py` 在终端里会当场问「回复署名 [当前/内置默认]: 」，回车沿用；也可以 `--signature "来自{owner}的 AI助手"` 或环境变量 `SIGNATURE` 直接给。值写进 `config.json` 的 `signature`，>60 字直接拒绝（在**任何写操作之前**校验，不会留下装了一半的状态）。

署名的默认值是一个**模板**：`此回复信息来自{owner}的 AI 助手`，其中 `{owner}` 渲染成这台机器当前登录的飞书账号姓名（与正文自称同一个来源、同一份实现，换账号下一条回复就跟着换，**不需要为每台机器写一遍人名**）。写成固定人名（如「来自某某的AI助手」）也照旧生效、字面值不改；取不到账号身份时 `{owner}` 回落成中性词「账号本人」。判据：`feishu-bridge doctor --assert` 的 `reply_identity` detail 里「署名「…」」打印的就是**渲染后真要发出去的那一行**。

**正文自称（本人姓名）**：装完**不需要任何身份参数**——正文里「你是<本人>的 AI 助手」会自动等于这台机器当前登录的飞书账号姓名（bridge 用 `target_open_id` 校验过的账号身份，运行时读取；换账号后下一条回复就跟着换，没有安装期写入的人名副本）。取不到账号身份时用中性自称「你是这个飞书账号本人的 AI 助手」，不会猜任何人名。判据：`feishu-bridge doctor --assert` 的 `reply_identity`（detail 会写明「来源：本机飞书账号身份」）。机制与判据的唯一权威见 [CONTRACTS.md](CONTRACTS.md)「身份与自称」。

**安装器会自己激活**（与 Windows 安装器的 D/F 阶段同一语义，2026-11-04 起）：`install.py` 写完 plist、切完 `current` 之后自动
`restart` 并等到新 release 的心跳刷新，回执里 `activation.bridge` 有 `pid`/`release`/`ok`。所以**不再需要手工 `feishu-bridge start`**；
装完 `launchd.loaded:false` 这种"升级成功但服务没起来"不会再出现。

| 退出码 | 含义 |
|---|---|
| 0 | 已安装并激活（心跳已是新 release） |
| 1 | 停在更早的前置检查/构建失败（没有任何写操作提交） |
| 3 | 代码已提交但没跑起来（激活失败，或 `--with-dsh-consumer` 的消费者 `live=false`）——按 stderr 的日志路径排查后重跑 |

**一条命令装完即验收**：

```bash
python3 install.py --verify      # 激活已在安装内完成；verify 只再跑 doctor --assert（有 fail 退出码 1）
```

**验收** `status`：`process_running=true`、`authenticated=true`、`running_release==installed_release`、`update_pending=false`、`workers` 里 `bridge/service.py:WORKER_NAMES` 的每一项都存活、`collector.watermark_lag_s` 为个位数秒。机器可读版本：`feishu-bridge doctor --assert`（`checks[]` 里逐项 ok/warn/fail，`verdict` 在 JSON 末尾）。

**挂起开关（下班 / 上班）**：

```bash
feishu-bridge hold -r "下班"     # 零采集/零入队/零回复；排队项丢弃并计数
feishu-bridge release            # 恢复；挂起窗口从回填时间轴上永久切掉
feishu-bridge status | grep -E 'mode|held|dropped'
```

也可在本人自聊会话里发「下班 / 上班」（需要该会话在 `self_chat_ids` 白名单里）。**注意**：如果 `update_pending=true`（装完新 release 但进程还跑旧版），`hold` 会直接拒绝并提示先 `restart` —— 旧进程不认这个开关，切了只会得到「看起来已下班」。

**日志**：`feishu-bridge logs`（业务日志最近 80 行）、`feishu-bridge logs --all --lines 200`（连 launchd/systemd 的 stdout/stderr 一起看，查导入错误/被杀用它）、`feishu-bridge logs -f`（跟踪）。

**常驻消费者（macOS）**：DSH 消费者由安装器托管，一条命令装/升：

```bash
python3 install.py --with-dsh-consumer --dsh-workspace "$HOME/dsh-workspace"
# 判据（三条都要）：
launchctl print "gui/$(id -u)/io.feishu-agent.dsh-consumer" | grep -E 'state = |pid = '
ps -o args= -p "$(launchctl print "gui/$(id -u)/io.feishu-agent.dsh-consumer" | awk '/pid = /{print $3}')" | grep -o 'current/dsh-consumer.py'
feishu-bridge consumer status     # live_consumers 里有 dsh-main 且 last_seen 新鲜
```

它做的事与 Windows 的 `dist/wsl2/wsl/install-dsh-consumer.sh` 一一对应：写单元（`io.feishu-agent.dsh-consumer`）、
`current/dsh-consumer.py` 作入口、显式 PATH（含安装器选中的 `dsh`）、默认 workspace `~/dsh-workspace`、
默认 consumer 名 `dsh-main`；写盘后 **bootout+bootstrap** 重载并等 `consumers.last_seen_at` 变新鲜才算成功。
`dsh` 不存在时安装器在写任何东西之前失败并给出 `npm install -g @deepseek-ai/dsh`；没登录/没 key 时
进程会起不来，回执 `activation.dsh_consumer.live=false`、退出码 3（按 stderr 提示补凭证）。
升级只需重跑同一条命令：单元指向 `current/`，重载即换代码（只 `kickstart` 不算）。

**升级带上 agentkit consumer 时**：consumer 的 plist 指向**绝对 release 路径**（故意不跟随 `current`，避免 release 里没打包 agentkit 时跑飞）。所以升 bridge 不升 consumer 是不行的：`python3 install.py --with-agentkit …（沿用原 consumer 名）` 之后必须 `python3 agentkit-consumerctl.py restart`，否则新 bridge 配旧 consumer。装 bridge 不带 `--with-agentkit` 时安装器会检测到并打 warning 指出旧 release id。

- 配置：`~/Library/Application Support/FeishuPersonalAgent/config.json`（`target_open_id`、`sending_enabled`、`app_daily_reply_cap`、各 interval）
- 事件超时：`feishu-bridge events timeout <秒>`（`external.json` 只剩 `reply_timeout_seconds`；范围与语义见 [CONTRACTS.md](CONTRACTS.md)），改完无需重启（按 mtime 读取）
- 日志：`bridge.log`（业务）、`logs/consumer.log`、`launchd.stdout/stderr.log`（解释器/监管层）；统一用 `feishu-bridge logs --all` 看
- 远程运维（Tailscale + OpenSSH）：[docs/ops-remote-manage.md](docs/ops-remote-manage.md)；脚本 `tools/bridge-remote.sh`
- 回滚：`feishu-bridge rollback --list` 看已安装版本，`feishu-bridge rollback --to <release_id>` 切回，再 `feishu-bridge restart`。切之前会校验旧 release 的 digest（损坏拒绝），并同步 `installation.json`（`update_pending` 不会因此变 true）。旧 release 必须与当前 schema 兼容（2.0 起 schema v6，v3 时代的 release 打不开新库：`user_version > 3` 直接 RuntimeError）；跨 schema 回滚要连库一起退到升级前的快照，不是只切 release。只保留最近 10 个 release（`FEISHU_RELEASE_KEEP=20` 放宽，`0` 关闭清理）
- 数据：`state.sqlite3`（WAL）——**不要放 SMB/网络盘**
- 回滚示例：`feishu-bridge rollback --list` → `feishu-bridge rollback --to 731bca111e124d17` → `feishu-bridge restart` → `agentkit-consumerctl.py restart`（消费者也要真的重载，否则还在跑旧 release 的路径）

---

## 3. DSH（会话式，推荐）

### 3.1 安装 skill

仓库 `skills/` 是唯一源，软链到用户级技能目录（DSH 会热发现）：

```bash
mkdir -p ~/.dsh/skills
ln -sfn "$HOME/Documents/feishu-personal-agent/skills/feishu-bridge-consumer" \
  ~/.dsh/skills/feishu-bridge-consumer
# 项目级也可：<project>/.dsh/skills/feishu-bridge-consumer
```

### 3.2 注册消费者（一次）

```bash
feishu-bridge events register --consumer dsh-main --kind dsh
feishu-bridge events claim --consumer dsh-main --limit 1 --lease 600 --kind message   # 空结果=正常
```

### 3.3 专用会话

1. 新建一个**专用** DSH 会话（不要复用日常会话）。
2. 输入 `/loop 30s`。
3. 粘贴 [`skills/feishu-bridge-consumer/SESSION-PROMPT.txt`](skills/feishu-bridge-consumer/SESSION-PROMPT.txt) 全文。

prompt 每轮：`claim --kind message --lease 600` → 读 `payload.text/context` → `events reply --event ID --body-file -`（自动 ack，别再 ack）→ 空结果立即结束；失败 `events fail`。

会话消费者也能检索和执行任务（与常驻入口同一能力模型）：可以读文件、搜索、联网、跑命令、改文件；不能删除、不能做破坏性操作、不能读取或外传密钥密码，不能委派子代理或用 MCP。会话式没有适配器的沙箱和流式兜底，只靠会话自身工具策略 + prompt，汇报时只写实际看到和做过的事。

### 3.4 验收

- 会话第一轮输出 `count=0`
- `feishu-bridge events status` 里 `dsh-main.last_seen_at` 在更新
- 真实 @ 一条：事件 `leased→done`、outbox `queued→reply_verified`、飞书只出现一条回复

> 会话式依赖会话存活；GUI/机器关闭即停。长期无人值守用 §4 或 §5 的常驻进程。

---

## 4. Codex（会话式 / Automation）

### 4.1 安装 skill

```bash
mkdir -p ~/.codex/skills ~/.agents/skills
ln -sfn "$HOME/Documents/feishu-personal-agent/skills/feishu-bridge-consumer" \
  ~/.codex/skills/feishu-bridge-consumer
ln -sfn "$HOME/Documents/feishu-personal-agent/skills/feishu-bridge-consumer" \
  ~/.agents/skills/feishu-bridge-consumer
```

### 4.2 运行

- **Codex Automation**：schedule 30s~1min，prompt 用同一段（把 consumer 名换成 `codex-main`）。
- 或 Codex 专用会话里 `/loop 30s` + prompt。
- 能力边界与 §3.3 相同：可检索、执行并完成任务，禁止删除/破坏性操作、密钥读取或外传、委派和 MCP；没有适配器兜底。

```bash
feishu-bridge events register --consumer codex-main --kind codex
```

### 4.3 验收

同 §3.4：空 claim、`last_seen_at` 更新、真实 @ 端到端、outbox `reply_verified`。

---

## 5. 事件消费者（只有一个引擎）

`bridge/consumer.py`（旧 Cursor 引擎 / `cursor-main`）、`cursor-consumer.py`、以及
`feishu-bridge consumer start|stop|restart|foreground` **已整体删除**；`install.py` 也不再写
cursor 的 LaunchAgent（升级时会顺手删掉遗留的 `io.feishu-agent.cursor-consumer.plist`）。
现在唯一的消费者运行时是 `consumer_runtime.py`，由各入口脚本注入 Agent：

```bash
feishu-bridge consumer status     # 只读清单：consumers / live_consumers / healthy / note
```

- 权威数据：`state.sqlite3` 的 `consumers` 表（每个消费者每轮 register 刷新 `last_seen_at`）；
  `consumer-status.json` 是已退役引擎的遗留文件，**不再参与任何判定**
- 私有 journal：`consumer-state/<消费者名>/<event>/<claim_token>/attempt.json`（先落盘答案再提交）
- 生命周期：macOS 是 LaunchAgent `io.feishu-agent.agentkit-consumer`，由随 release 分发的
  `agentkit-consumerctl.py`（`status` / `register` / `start` / `restart` / `stop`）管理；
  **换 release 后必须 `restart`**（bootout+bootstrap），只 `kickstart` 会让进程继续指向旧 release
- 门禁：`sending_enabled=false`（pause）、`target_open_id` 在进程启动后被改、或 `hold.json` 挂起中，
  消费者一律不领事件；`config.json` 读不到时消费者启动直接失败
- 回滚：release 回滚 + 重启（见 §2 的回滚说明），不再有"装回 cursor-main"这条路

---

## 6. DSH / Codex / 自研 Agent（常驻进程式）

仓库提供三个入口和一套模板。**macOS 的 DSH 消费者是安装器托管的常驻服务**（`install.py --with-dsh-consumer`，
见 §2）；Windows/WSL2 由 `dist/wsl2/wsl/install-dsh-consumer.sh` 托管。其余入口/自研 Agent 自己部署：

| 入口 | consumer 默认名 | 说明 |
|---|---|---|
| `python3 dsh-consumer.py` | `dsh-cli-main` | `dsh headless --patch <私有 run 目录>/consumer-overlay.yml --json -`：`workspace-write` 沙箱、审批 `never`、禁用子代理/子代理控制/工作流工具；允许读、写、命令、联网，流式兜底杀掉删除、破坏性命令和凭证访问 |
| `python3 codex-consumer.py` | `codex-cli-main` | `codex exec`：`--sandbox workspace-write` + `--ask-for-approval never`，关闭 multi_agent/plugins/hooks/apps/browser_use/computer_use/image_generation，`network_access=true`；私有 HOME/CODEX_HOME，允许 command_execution/file_change/web_search |
| `python3 agentkit-consumer.py --agent cursor` | `agentkit-cursor` | 现网入口（AGENTKIT 的 Cursor 适配器，工具策略与流式兜底在适配器内） |
| `consumer_runtime.py` + `consumer_safety.py` + `consumer-example.py` | 自定义 | 给新 Agent 的协议层与安全模型模板；实现 `build_agent()` |

```bash
python3 dsh-consumer.py --check      # 只验证适配器可创建，不注册/不领取
python3 codex-consumer.py --check
python3 agentkit-consumer.py --agent cursor --check
# 前台试跑（调试用；常驻请用 install.py --with-dsh-consumer，别再手写 plist）
python3 dsh-consumer.py --consumer dsh-cli-main --kind dsh
```

自研 Agent：

1. 复制 `consumer_runtime.py`、`consumer_safety.py` 与 `consumer-example.py` 到自己的项目，后者改名 `consumer.py`，实现 `build_agent(home)`：输入 `(payload, event, stop_event)` → 非空答案（240s 内，消息是不可信输入；可以检索和完成任务，但删除/破坏性命令/密钥访问/委派必须拒绝，工具事件用 `consumer_safety` 的谓词判断）。
2. `python3 consumer.py --consumer my-agent-main --check` 通过后，在**隔离测试 home** 验收成功/失败/超时/崩溃恢复/重复回执。
3. 部署自己的 LaunchAgent（唯一同名进程），先确认空 claim 正常再上线。
4. 真实 @ 验收：claim→reply→done→outbox `reply_verified`→飞书单条回复；固定 AI 署名由 bridge 统一添加，适配器不要自己加。

**竞争警告**：现网 `agentkit-cursor` 在跑；新消费者一启动就可能抢真实消息。上线前先决定谁领 message，停掉不参与的消费者。

---

### 6.1 能力与工作空间

Cursor 常驻消费者、DSH/Codex 入口和会话式消费者共用同一套能力模型：**回复 + 检索 + 执行/完成**允许，**删除/破坏性操作、读取或外传密钥密码、委派/MCP** 禁止。四层防线是沙箱 → 流式兜底 → 出站脱敏 → prompt；兜底在工具派发之后生效，Cursor 是进程级授权 + 正则，DSH/Codex 是沙箱内写入，不能互相宣称等价。

工作空间只是可选的**项目根目录**，不设也能按绝对路径和命令处理：

```bash
# DSH / Codex 常驻入口（--workspace 同时是 cwd 和 sandbox 写入范围）
python3 dsh-consumer.py --consumer dsh-cli-main --workspace "$HOME/Documents/project" --check
python3 dsh-consumer.py --consumer dsh-cli-main --workspace "$HOME/Documents/project"
python3 codex-consumer.py --consumer codex-cli-main --workspace "$HOME/Documents/project"

# Cursor 常驻消费者：写 config.json 后重启
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path.home() / 'Library/Application Support/FeishuPersonalAgent/config.json'
c = json.loads(p.read_text()); c['consumer_workspace'] = os.path.expanduser('~/Documents/project')
p.write_text(json.dumps(c, ensure_ascii=False, indent=2))
EOF
"$HOME/Library/Application Support/FeishuPersonalAgent/current/agentkit-consumerctl.py" restart
feishu-bridge consumer status     # 输出里出现 workspace
```

- 会话式：在该项目目录打开专用 DSH/Codex 会话，再 `/loop 30s` + prompt，按 skill 的「能力与工作空间」规则检索、执行并如实汇报。
- Cursor 常驻消费者：cwd 仍是私有 runtime（策略文件所在），工作空间以 `--add-dir` 加入，不是把 cwd 换过去。不设 workspace 时仍可用绝对路径和命令处理问题。cli.json 允许读/搜索/联网/Shell/写入/修改，拒绝 `Task`/`Mcp`/`Delete`；它只是尽力而为，真正兜底是流式工具事件阻断。
- DSH 入口：`workspace-write` 沙箱，读写/命令/联网都允许，写入被限制在 workspace（不设则落在私有 run 目录）；每轮 overlay 含 `consumer` permission preset 并禁用子代理和工作流工具。凭据顺序：env `DEEPSEEK_API_KEY` → `$DSH_HOME/.credentials.yaml`（已 `dsh` 登录，overlay 按次复用）→ `.env`；都没有则在 claim 前报错。
- DSH 走**非官方 provider**（公司内部 OpenAI 兼容网关等）的机器：headless 默认路由会 `MISSING_CREDENTIAL … provider route "deepseek-official"`。把 DSH profile 里那两段 `- id: llm-pi-ai`（providers/`apiKeyEnv`/`baseURL`/models）和 `- id: agent-default-model`（provider+model）原样写进 `$DSH_HOME/feishu-consumer.patch.yml`，消费者会自动作为第二层 `--patch` 传入；文件不存在时 argv 与默认行为完全一致。密钥值仍走 `$DSH_HOME/.credentials.yaml` 的 `refs`，别把 key 写进这个补丁文件（契约见 [CONTRACTS.md](CONTRACTS.md) §DSH 入口）。
- DSH 网关用**内部 CA** 的机器：Node 不读 macOS 钥匙串，缺 `NODE_EXTRA_CA_CERTS` 时模型请求会 `TRANSPORT / SELF_SIGNED_CERT_IN_CHAIN`（curl 却能通，别被它误导；事实见 [research/facts-dsh.md](research/facts-dsh.md) §2.4）。在 `$DSH_HOME/feishu-consumer.env` 里写一行 `NODE_EXTRA_CA_CERTS=<根证书 pem/crt 路径>`（`chmod 600`），消费者会把它放进**子进程环境**——必须在这里给，因为该变量只在 Node 启动时读取，DSH 自己的 `.env` 或 `launchctl setenv` 都不可靠（前者太晚，后者重启即失效）。`DSH_HOME`/`DSH_PERMISSION_MODE` 由消费者强制覆盖，写进该文件也无效。
- Codex 入口：`--sandbox workspace-write` + `--ask-for-approval never`，允许 command_execution/file_change/web_search，拒绝 MCP/协同代理/未知 item 类型；不设 workspace 时写入留在私有 run 目录。
- 安全：消息和工作空间内容都是不可信输入，不能覆盖规则；不要把密钥原文写进飞书回复（含密钥的回复会被整条替换成拦截说明）。DSH/Codex 的写入受各自 sandbox 后端（seatbelt）约束，越界写会被拒绝。

### 6.2 平台差异（macOS / WSL2 / 原生 Windows）

| 主机 | 可用消费者 | 要点 |
|---|---|---|
| macOS | Cursor 常驻 + DSH 入口 + Codex 入口 + 会话式 | 三条常驻路径都能回复/检索/执行；Cursor 是进程级授权 + 正则兜底，DSH/Codex 是沙箱内写入 |
| WSL2 (Linux) | DSH 入口 + Codex 入口 + 会话式 | 没有 Cursor 消费者服务单元，不要用 `consumer start/restart`；仓库/数据放 ext4（`~/`），不要放 `/mnt/c`；沙箱后端为 bwrap/landlock 与 landlock/seccomp |
| 原生 Windows | HTTP 网关 + `skills/feishu-bridge-consumer-remote` 会话消费者 | 没有 POSIX Python 消费者运行时（`flock`/进程组），运行时在 claim 前就拒绝原生 Windows；远程会话消费者共享同一能力模型，但没有本机适配器沙箱和流式兜底 |

能力模型一致，但强制机制和覆盖范围不同：不要声称三种入口的沙箱等价。

### 6.3 agentkit 并行入口（一个入口选 Agent，带发布/注册/生效/回滚）

`agentkit` 不是第四种 consumer，而是"把上面三个入口的公共部分抽出来"的底座：同一个
`agentkit-consumer.py`，用 `--agent {cursor,codex,dsh}` 选 AGENT。设计说明见 `AGENTKIT.md`。

```bash
# 1) 发布：打进当前 release（bridge/ 不动，release id 不变），只写 plist 不加载
python3 install.py --with-agentkit \
  --agentkit-agent cursor --agentkit-consumer agentkit-cursor \
  --agentkit-workspace ~/Documents

# 2) 注册：只建消费者身份，不 claim
python3 agentkit-consumerctl.py status
python3 agentkit-consumerctl.py register

# 3) 生效：加载 LaunchAgent（幂等）
python3 agentkit-consumerctl.py start

# 4) 换 release / 换代码之后：真重载（bootout+bootstrap；只 kickstart 会继续跑旧 release）
python3 agentkit-consumerctl.py restart

# 5) 停用（把 plist 挪进 <home>/disabled-consumers/，登录不再自启）
python3 agentkit-consumerctl.py stop
```

要点：

- `ctl` 随 release 一起分发：`releases/<id>/agentkit-consumerctl.py`（可执行），装好的机器上直接能跑，
  不需要仓库副本。
- **`restart` 必须是 bootout+bootstrap**：`install.py` 改写 plist 里的 release 路径后，
  `launchctl kickstart -k` 仍按 launchd 缓存的旧参数起进程（plist 里是新路径、进程 argv 里是旧路径）。
  判据：`ps -o command= -p <pid>` 里的 release 目录是否等于 `status.running_release`。
- 旧的 cursor 引擎已删除，`start` 不再需要 `--take-over`，`stop` 也不再"恢复 cursor"：
  回到上一个版本用 `feishu-bridge rollback --to <release_id>` + `feishu-bridge restart`。
- 发布后可以先"只看不领"自检：`python3 agentkit-consumer.py --agent cursor --check --home <home>`，
  它打印能力声明、二进制路径、session 目录，**不 register、不 claim**。
- 一次真实接管的验收口径（本机 2026-11-01 已过）：事件 `claims/acks` 递增、`events status` 里
  那条从 `leased` 变 `done`、飞书出现单条回复（`reply_to` 指向原消息）、
  `<workspace>/.agentkit/runs/<run_id>/run.json` 的 `status=completed` 且有 `served_model`。
- **重启安全**：停用的 plist 在 `<home>/disabled-consumers/`，`~/Library/LaunchAgents/` 里只应该有一个
  consumer plist；多于一个意味着登录时双开（两个 consumer 抢同一批事件）。
- 备份/回滚材料在 `~/Library/Application Support/FeishuPersonalAgent/backups/agentkit-rollout-<ts>/`。


## 7. 路由与竞争（重要）

```bash
feishu-bridge events status     # 各 consumer 的 claims/acks、事件积压、回复超时
```

- 多个消费者都 claim `message` → 谁先抢到谁处理（同一事件只有一个 owner+claim_token）。
- 想让某 Agent 独占：停掉其它 message 消费者（`agentkit-consumerctl.py stop` / 会话 `/loop` 停掉 / 只订阅 attention）。
- 没有消费者就没有回复：事件停在 `external_queued`，`doctor --assert` 的 `consumer_offline` 硬失败。
- 消费者领了事件却没回复的宽限期由 `feishu-bridge events timeout <秒>` 决定（默认与范围见 [CONTRACTS.md](CONTRACTS.md)），超时事件记 `dead` + `needs_attention`。

---

## 8. 验收清单（每个 Agent 都要过）

- [ ] 先 `~/.local/bin/feishu-bridge resume`：新装 `sending_enabled=false`，不 resume 时 sender 被门控、回复发不出去，验收必失败（`status` 里看 `sending_enabled`）
- [ ] `feishu-bridge status`：authenticated / workers / `update_pending=false`
- [ ] `feishu-bridge doctor --assert` 退出码 0（`checks` 里没有 fail；worker 全活 / 积压 / `consumer_offline` 都在里面）
- [ ] `events register` 成功，空 claim 返回 `count=0`（不报未注册/锁冲突）
- [ ] 真实 @：该 consumer claim 到 → reply 成功 → 事件 `done` → outbox `reply_verified` → 飞书**只有一条**回复
- [ ] 重复 reply 返回 `duplicate:true`（幂等）
- [ ] 失败路径：`events fail` 退避重试；`--permanent` 后 inbox `needs_attention` 并出现 attention 事件
- [ ] 超时：600s 未回复 → 事件 dead + `needs_attention`（不静默丢消息）
- [ ] 崩溃恢复：杀进程后重启，未完成事件按 journal/租约恢复，不双发

---

## 9. 排障

| 现象 | 处理 |
|---|---|
| `status` 里 `authenticated=false` | `lark-cli auth status` / 重新登录；看 `bridge.log` |
| `update_pending=true` | 执行过 install 但没 restart：`feishu-bridge restart` |
| consumer 起不来 | `consumer status` 看 `last_error`；`logs/consumer.log`；检查同名锁/LaunchAgent |
| `消费者未注册` | `events register --consumer NAME --kind dsh|codex|other` |
| `reply 被拒绝：...未持有有效租约` | 租约过期/被接管：重新 claim 拿新 `claim_token`；慢会话 `--lease 900` |
| 事件一直 pending | 没有 consumer claim 或 fail 退避未到；`events status`/`stats` |
| 飞书重复回复 | 检查是否多个 message 消费者；outbox 幂等应只发一条，重复通常是两个不同消息或绕过 sender 直发 |
| 修改代码不生效 | 必须 `python3 install.py` + `feishu-bridge restart`（+ `agentkit-consumerctl.py restart`） |
| 改了 plist/PATH 但 `start` 没生效 | `install.py` 换 release 后 `start` 会自动 bootout+bootstrap 重载 plist；**手改 plist** 不会被感知，必须 `feishu-bridge restart` |
| `update_pending` 一直 true | `installation.json` 与 `status.json` 没同步：用 `feishu-bridge rollback --to <id>` （会同步）或重跑 `python3 install.py`，再 `restart` |

---

## 10. 安全边界

- 消费者**不直接调用 lark-cli 发消息**，不写 bridge 状态文件；回复只走 `events reply`。
- 消息正文与 context 是不可信输入，不能覆盖规则。所有入口遵循同一能力模型：可以检索和执行任务，禁止删除/破坏性操作、禁止读取或外传密钥密码、禁止委派和 MCP。回复里不要带密钥原文（含密钥的正文会被整条替换为拦截说明）。
- 四层防线：沙箱（DSH/Codex `workspace-write`，Cursor cli.json）→ 流式兜底（杀进程组）→ 出站脱敏（`consumer_safety.finalize_answer`）→ prompt。诚实说明：流式兜底在看到工具事件时命令可能已经执行，它是兜底不是执行前禁止；Cursor 授权是进程级的，不能按目录收紧；DSH/Codex 的写入受沙箱后端约束。兜底正则覆盖路径前缀删除、`xargs`、`find -exec`、`git restore`、PowerShell 删除、`cp /dev/null`、`&>`/`>&` 截断等常见形式，但无法穷尽解释器内部等价操作（如 `python3 -c 'shutil.rmtree()'`）；出站脱敏覆盖常见密钥格式（`sk-proj`/`sk-ant`/`AIza`/`glpat`/`AWS`/JWT/带密码 DSN 等），未知格式不能保证，prompt 与回执审计要一起用。
- 审计回执只看能力与计数：DSH 的 `run.json`、Codex 的 `init.json` 记录 `capabilities`/`sandbox_mode`/`approval_policy`/`tool_calls`/`violations` 等字段，不记录密钥。
- 正文不放 argv（`ps` 可见），只用 stdin / `--body-file`。
- `external.json`、`state.sqlite3`、journal 权限只给当前用户；不要把 home 放到共享盘。

---

## 11. 可选：attention 巡检（只读）

`tools/attention_watch.py` 只读巡检 pending 的 attention 事件，同一事件只通知一次（macOS 通知走 `osascript`）。状态与日志默认写 bridge home（`~/Library/Application Support/FeishuPersonalAgent`），不写仓库工作区，`git clean`/重 clone 不会丢状态。

```bash
python3 tools/attention_watch.py --dry-run   # 只打印，不通知、不落盘

# 装成每 600s 跑一次的 LaunchAgent（仓库内没有现成 plist，模板如下）
mkdir -p ~/Library/LaunchAgents
sed -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" > ~/Library/LaunchAgents/io.feishu-agent.attention-watch.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.feishu-agent.attention-watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>__REPO__/tools/attention_watch.py</string>
  </array>
  <key>WorkingDirectory</key><string>__REPO__</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>600</integer>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>__HOME__</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>StandardOutPath</key><string>__HOME__/Library/Application Support/FeishuPersonalAgent/attention-watch.stdout.log</string>
  <key>StandardErrorPath</key><string>__HOME__/Library/Application Support/FeishuPersonalAgent/attention-watch.stderr.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.feishu-agent.attention-watch.plist
launchctl kickstart -k gui/$(id -u)/io.feishu-agent.attention-watch
```

它不 claim、不 ack、不写 bridge 状态库；只适合"提醒人去看"，真正的处理仍由人工 `events ack`。
