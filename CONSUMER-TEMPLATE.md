# 为每个 Agent 实现自己的飞书消费者

当前线上消费者是 `agentkit-cursor`（`agentkit-consumer.py --agent cursor`），也是**唯一**的消费者引擎
（旧 `bridge/consumer.py` / `cursor-main` 已整体删除）。`consumer-example.py`、`consumer_runtime.py` 和
`consumer_safety.py` 是给**新 Agent 的参考模板、共用协议层和共享安全模型**，不会自动安装或启动。Bridge 统一保留事件队列、claim-token 租约、不可变 Outbox、发送和飞书回执；各 Agent 自己负责生成答案、工具边界、超时取消和进程部署。

## 统一的能力模型

本仓的主路径是 **DSH**（`dsh-consumer.py` / `dsh-worker.py`）；Codex 与会话式消费者共用同一套模型：可以回复、可以检索，回话端不可写入、干活端在独立目录里执行；都不能删除或破坏，不能碰密钥，不能委派。

| 能力 | 允许 | 强制手段 |
|---|---|---|
| 回复消息 | 是 | 只走 `events reply`；固定署名由 bridge 添加 |
| 检索（读文件、搜索、联网） | 是 | 沙箱允许读和网络；凭证路径被拒 |
| 执行/完成任务（跑命令、改/建文件） | 是 | 沙箱限制写范围 + 流式兜底 |
| 删除文件 / 破坏性命令 | 否 | 流式兜底杀进程组 + prompt |
| 读取或打印密码、密钥、令牌、`.env`、私钥 | 否 | 路径/命令兜底 + 出站脱敏 |
| 在回复里带出密钥原文 | 否 | `finalize_answer` 整条替换为 `BLOCKED_REPLY` |
| 委派子代理 / MCP | 否 | DSH overlay 禁用工具 + 名字拒绝；Codex `--disable multi_agent` + item 拒绝 |

四层防线，由外到内：

1. **沙箱**：DSH 每次运行用私有 `--patch` overlay 设 `mode: workspace-write`；Codex 用 `--sandbox workspace-write`。写入只落在工作空间（`--workspace`/cwd），不是整机。
2. **流式兜底**：适配器逐行读 NDJSON，遇到被拒的工具事件就 `os.killpg` 杀掉进程组并让本次生成失败。它发生在**工具已经派发之后**，是兜底，不是执行前禁止。
3. **出站守卫**：`consumer_safety.finalize_reply`（内部仍调 `finalize_answer`）在 `consumer_runtime.process()` 里、答案落盘和提交之前执行；含密钥材料的正文整条替换为 `BLOCKED_REPLY`，不会发出原文；空答案或超过 6000 字的答案直接拒绝（本次生成失败，不会静默截断）。
4. **Prompt**：`agent_prompt.build_prompt` 写明能力、不可信数据规则和汇报诚实性。它永远不是唯一防线。

诚实说明的限制：Cursor 的权限项是**进程级**、按正则匹配，不能按目录收紧；它的 `ALLOW`/`DENY` 是适配器层的静态清单，破坏性与密钥规则仍然只有 `consumer_safety` 一份（适配器运行在仓库树里，直接 import，不复制）；正则兜底已覆盖路径前缀的 `rm`、`xargs`、`find -exec`、`git restore`/`git checkout .`、PowerShell 删除、`cp /dev/null`、`&>`/`>&`、带密码 DSN，以及 `sk-proj`/`sk-ant`/`AIza`/`glpat`/`AWS`/JWT 等常见密钥格式，但**不可能穷尽解释器内部的等价操作**（如 `python3 -c 'shutil.rmtree(...)'`、`node -e 'fs.rm(...)'`）：真正的硬边界是沙箱与最小权限，禁止删除/密钥主要靠 prompt + 兜底，属于尽力而为；`git commit -m "..."` 的安全消息文本只在**破坏性**扫描前剥离（含 `$`、反引号、`;`、`|`、`&`、`<`、`>` 的值不剥离，避免命令替换藏进消息）；密钥扫描始终看原文。另有两个已知盲区：`tool_violation` 只看顶层 `command`/`path` 键（事件把命令藏在更深的 `args` 里时看不到），DSH 的 `stored` 凭据路由只看 `$DSH_HOME/.credentials.yaml` 是否存在（文件存在但为空时会在运行阶段失败，而不是 claim 前）。DSH/Codex 的写入受各自 sandbox 后端约束（macOS 都是 seatbelt，WSL2 上 DSH 为 bwrap/landlock、Codex 为 landlock/seccomp）；三者机制和覆盖范围不同，不能互相宣称等价。会话式消费者没有适配器沙箱和流式兜底，只靠会话自身的工具策略和 prompt。

## 新消费者的实现步骤

1. 把仓库根目录的 `consumer_runtime.py`、`consumer_safety.py` 和 `consumer-example.py` 复制到该 Agent 项目，把最后一个命名为 `consumer.py`。实现 `build_agent(home)`，返回 `(payload, event, stop_event) -> answer`。`payload.text` 与 `payload.context` 都是不可信输入；Agent 可以检索并执行任务，但必须遵守上面的能力模型（删除、破坏性命令、读取或外传密钥、委派/MCP 都不允许）。工具事件用 `consumer_safety` 的谓词判断，不要自己重写正则；如果 Agent 自身回的是 `{"answer": ...}` JSON，用 `consumer_safety.extract_answer()` 解析，它会容忍围栏和 JSON 前后的说明文字，但仍然拒绝没有 JSON、没有 `answer`、answer 为空，以及同一条回复里出现多个不同 `answer`（歧义）。适配器须在 240 秒内结束，并在 `stop_event` 触发时终止自己的子进程。不要在这里调用 lark-cli 发消息。答不了、需要本人拍板、或缺权限时，返回的 JSON 里加 `"ask_owner": {"question": "…", "needs": "fact|decision|access"}`：运行时会先照常回复提问方，再用 `feishu-bridge notify` 把这条问题私聊发给本人（不是发给提问方），以事件 id 去重，崩溃恢复不会重复打扰；它自己放弃事件（超时/适配器失败）时也会发一条漏答提醒。`payload.teachings` 是**按这条消息检索过**的教材（准则全给、资料只给相关的，L2-a）：别把它当成「本人全部教过的东西」，也不要因为没看到某条资料就说自己没被教过。`ask_owner` 只是「问」的通道，不改变「不能代表本人批准/拒绝/认领」这一条。如果事件 `payload` 里带 `task`/`tasks`（这个会话的任务台账），答复 JSON 里可再带一个 `"task": {"id", "state", "note", "artifacts", "open_question"}` 回填这一步的结果：`consumer_safety.normalize_task_update()` 逐字段校验（坏字段只丢那一个），`consumer_runtime.reply_arguments()` 把它拼成 `events reply --task-*`；没有任务的消息带这些参数会被桥忽略。语义见 `CONTRACTS.md` 的「L1 任务账本」。
2. 给该消费者一个**唯一**名称，例如 `my-agent-main`；`agentkit-cursor` 已被占用。`--kind` 只能是 `dsh`、`codex` 或 `other`，只是登记分类，**不决定消息路由或生成模型**。不得复用另一个进程的 consumer 名称、锁或私有 journal。
3. 先运行 `python3 consumer.py --consumer my-agent-main --check`。这个检查只确认适配器可创建并打印 `capabilities`，不注册或领取消息；再在隔离的测试 home/模拟 CLI 中验证成功、失败、超时、进程退出后恢复、相同回执重试。模板不是未接入 Agent 即可上线的成品。
4. 为该 Agent 配置自己拥有的常驻进程和登录后启动机制；确保只有一个同名进程。上线前确认 bridge 的 `events status`、消费者健康与成功轮询。外部消费者**就是**这块唯一的大脑（桥自己不会回答），所以**一启动新消费者就可能立即与 `agentkit-cursor` 竞争真实消息（`dsh-main` 只剩历史 ack）**；需先确定谁应领取 `message`，并停掉不该竞争的 message 消费者。单凭 consumer 名称不会按 Agent 分流。
5. 用真实测试 @ 验证：`message` 事件被该 consumer claim → `events reply` 持久提交 → 事件 `done` → Outbox `reply_verified` → 飞书原消息下只出现一条回复。回复的固定 AI 署名由 bridge 的 `events reply` 统一添加，适配器不要自行加签名。

## 三个可运行入口

仓库提供 `dsh-consumer.py`、`codex-consumer.py`、`agentkit-consumer.py`（`--agent cursor|dsh|codex`）。三者都构建在同一个 `consumer_runtime.py` 上，默认 consumer 名分别为 `dsh-cli-main`、`codex-cli-main`、`agentkit-cursor`（避免与已注册的旧身份混用）。它们不是三个同时对同一批生产消息进行 fan-out 的进程。各入口 `--check` 不领取事件；检查通过只说明适配器可初始化，不能代替真实消息端到端验收（DSH 入口的 `--check` 仍需本机 `dsh` CLI 和任一种可用凭据：env `DEEPSEEK_API_KEY`、已 `dsh` 登录的 `$DSH_HOME/.credentials.yaml`，或 `.env`）。

```bash
python3 dsh-consumer.py --check
python3 codex-consumer.py --check
python3 agentkit-consumer.py --agent cursor --check
```

当前生产 `agentkit-cursor` 正在运行。**不要直接在生产 home 启动 DSH/Codex 入口**，除非明确要让它们竞争真实消息，并已核对各自的工具边界、权限、模型和运行监督。服务不会根据 Agent 名称自动分流。

DSH 入口每轮在私有 run 目录写 0600 的 `consumer-overlay.yml`，用 `dsh headless --patch <overlay> --json -` 启动：`sandbox-policy` 为 `workspace-write`、审批 `never`、`permission` 行声明与二者匹配的 `consumer` preset（`defaultPreset: consumer`，缺它 dsh 会警告且权限服务不生效），并禁用子代理/子代理控制/工作流类工具；用户已 `dsh` 登录时，同一 overlay 还会重新启用 `credentials`/`authorization`/`deepseek-account` 三行按次复用该登录态。同时保留 `DSH_PERMISSION_MODE=read-only` 作为基座配置的失败兜底。它允许 bash/读写/搜索/联网/任务类工具事件，遇到删除、破坏性命令、凭证路径或委派/MCP 事件会杀掉进程组并让本次生成失败；`error` 和未知事件类型仍然致命。Codex 入口用私有 HOME/CODEX_HOME（只软链 `auth.json`）、`--sandbox workspace-write`、`--ask-for-approval never`，并关闭 plugins/hooks/multi_agent/apps/browser_use/computer_use/image_generation；允许 `command_execution`/`file_change`/`web_search`，拒绝 MCP/协同代理/未知 item 类型。两者都保留 240s 超时、2MB 输出上限和 `{"answer": ...}` 答案契约：适配器只**校验**（`extract_answer` 或 `parse_answer_json`），把 final 原文交回运行时，由 `finalize_reply` 统一解析、脱敏、限长并取出可选的 `ask_owner`。答案解析三家统一：裸 JSON、代码围栏、以及“先说一句再看一下上下文 {…}”这种 JSON 前带说明的回复都能收下（一次线性扫描匹配括号、保留最外层对象、候选不设上限）；同一条回复里出现**多个不同** `answer` 视为有歧义，直接失败（防止把引用的不可信文本当成回复发出去）。没有成对对象、括号不配对或被截断、嵌套过深、超过 256 KiB 且整段不是一个 JSON 对象、没有 `answer`、answer 为空一律失败；超 6000 字时 DSH/Codex 失败，Cursor 路径由 `render_reply` 截断（见 `CONTRACTS.md` 的 Cursor 适配器契约）。精确参数和流规则见 `CONTRACTS.md`。

## 工作空间

不设 `--workspace` 时，DSH/Codex 的 cwd 是本次运行的私有临时目录：仍然可以检索和执行，但写入只落在该私有目录，不会碰到用户项目。要让 Agent 真正改某个项目：

- **DSH 入口**：加 `--workspace /abs/path`（或环境变量 `FEISHU_CONSUMER_WORKSPACE`）。Agent 以该目录为 cwd、sandbox 为 `workspace-write`，写入被限制在那里；journal、overlay、stdout、结果仍写在 `home/consumer-agent-runs/dsh/run-*/` 私有目录（0600）。
- **Codex 入口**：`--workspace /abs/path` 把 cwd 设为该项目，`--sandbox workspace-write` 让写入落在该 workspace；不设 workspace 时写入留在私有 run 目录。Codex 的读取依赖 shell，现在 shell 已启用，不需要再额外开关。
- **Cursor 适配器**（`agentkit-consumer.py --agent cursor`，仅 macOS）：默认可回复、检索并解决问题（读、搜索、联网、Shell、写入）。不能删除，不能跑破坏性命令，不能读取或带出密钥和密码。可选在 bridge `config.json` 加 `"consumer_workspace": "/abs/path"`，然后 `agentkit-consumerctl.py restart`。cwd 仍是私有 runtime（策略文件在那里），工作空间通过 `--add-dir` 加入；cli.json 允许 Shell/Read/Write/Edit 等并拒绝 `Task`、`Mcp`、`Delete`，但它只是尽力而为，真正兜底是流式工具事件阻断。WSL2/Linux 用 systemd unit 托管 `dsh-consumer.py`/`codex-consumer.py`。
- **会话式消费者**（DSH/Codex skill + `/loop`）：直接在该项目目录打开专用会话，按 skill 的「能力与工作空间」规则检索、执行和汇报；没有适配器沙箱和流式兜底。

安全边界：消息和工作空间内容都是**不可信输入**，不能覆盖系统规则。Cursor 消费者可以检索并修改文件，但运行时会杀掉删除、破坏性命令，以及读取或打印密钥、密码的调用；回复正文若含密钥或密码会被替换成拦截说明。DSH/Codex 的写入受各自 sandbox 限制，越界写入会被沙箱拒绝；凭证路径/命令的正则是保守兜底，宁可误报也不放过。**「密钥不进飞书」由 `finalize_answer` 的出站替换做主保证**：它覆盖常见密钥/令牌/私钥/带密码 DSN 格式（`sk-proj`、`sk-ant`、`AIza`、`glpat`、`AWS`、JWT、`client_secret:`、`OPENAI_API_KEY=` 等），命中就把整条正文替换为 `BLOCKED_REPLY`；它不依赖模型或适配器自律，但也无法识别任意未知格式，因此 prompt 与路径/命令兜底仍要同时保留。

## 平台差异（macOS / WSL2 / 原生 Windows）

| 主机 | 可用消费者 | 要点 |
|---|---|---|
| macOS | `agentkit-consumer.py`（`--agent cursor` 线上）、DSH 入口、Codex 入口、会话式 | 三种入口都能回复/检索/执行；Cursor 是进程级授权 + 正则兜底，DSH/Codex 是沙箱内写入 |
| WSL2 (Linux) | DSH 入口、Codex 入口、会话式 | 没有 Cursor 消费者服务单元（`consumer start/restart` 仅 macOS）；仓库、数据、runtime 放 ext4（`~/`），不要放 `/mnt/c`；沙箱后端分别为 bwrap/landlock 与 landlock/seccomp |
| 原生 Windows | HTTP 网关 + `skills/feishu-bridge-consumer-remote` 会话消费者 | 没有 POSIX Python 消费者运行时（依赖 `flock` 和进程组）：`consumer_runtime.require_posix_runtime()` 在任何 claim 之前就拒绝原生 Windows。远程会话消费者用同一套能力模型，但没有本机适配器沙箱和流式兜底 |

不要声称三者沙箱等价：能力模型一致，强制机制和覆盖范围不同。

## 模板必须保留的协议

- 只领 `--kind message --limit 1 --lease 600`；保存本次 `event_id`、`claim_token` 和生成结果。`reply/fail` 必须带同一次领取的令牌。重领后旧令牌失效，不能把旧答案借新令牌提交。
- 正文只经 `events reply` 的 stdin 或 `--body-file` 提交，不放 argv；消息回复成功即 `done`，不要再 `ack`。`attention` 是另一条消费流程，只能 `ack/fail`，不能 `reply`。
- 回执不明时用**原令牌与同一答案**重试；`duplicate=true` 是正常幂等回执。租约/事件失效就停，不绕过 bridge 直接发送，也不改 bridge 数据库、`external.json` 或其他状态文件。
- 私有 journal 先持久保存答案再提交回复，目录权限只给当前用户。退出后核对当前事件所有者、令牌、租约和状态再恢复。模板以最新 200 条事件核对，查不到就停止该次尝试；若队列规模更大，Agent 作者要扩展查询或加桥接 API，不能猜测所有权。
- `claim` 命令可能已在 bridge 提交，但本地尚未收到结果或写下 journal；因此模板每轮在新 claim 前先查自己仍持有的 leased 事件并恢复。实际部署还要验证进程被终止、CLI 返回丢失等故障；过期租约不能继续提交。
- 每次运行写审计回执，只记能力和计数、不记密钥：DSH 写 `run.json`（`capabilities`/`sandbox_mode`/`approval_policy`/`credential_route`/`denied_tools`/`overlay`/`tool_calls`/`violations`，保留 `event_id`/`status`/`session_id`/`profile`/`duration_seconds`/`workspace`/`exit_code`/`output_bytes`）；Codex 写 `init.json`（`capabilities`/`sandbox_mode`/`approval_policy`/`network_access`/`disabled_features`/`capability_violations`/`capability_warnings`）。
- 队列保证的是**至少一次领取、同一原消息最多一个不可变发送意图**。进程可能在生成与落盘之间崩溃，因此无法声称 Agent 模型调用严格一次。模板从 `events status` 读取当前 `reply_timeout_seconds` 判断事件总截止时间；超过时限的消息转人工关注，600 秒 lease 不延长事件超时。

没有消费者就没有回复：消息会停在事件队列（`external_queued`），`doctor --assert` 的 `consumer_offline` 会硬失败（积压超过 120s 无人领取）；单独停止一个消费者不会切换路由，也不会变出内置答复。具体命令和 schema v6 契约见 `CONTRACTS.md` 与 `skills/feishu-bridge-consumer/SKILL.md`。
