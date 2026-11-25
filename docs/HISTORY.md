# 变更与事故账（只记「以后会再遇到」的）

> 这份文件替代了以前散落在根目录的一次性快照（`WINDOWS-PORTING-2026-10-27.md`、
> `WINDOWS-RETRO-2026-10-28.md`、`WINDOWS-FEEDBACK-TEMPLATE.md`、`PENDING-RELEASE-20261031.md`、
> `DESIGN-ACK-THEN-FOLLOWUP-20261031.md`、`reviews/**`）。原文仍在 git 历史里（用
> `git log --diff-filter=D --name-only -- <路径>` 找回），这里只留**结论与判据**。
>
> 判据优先于叙事：每条都写「怎么判断」「怎么修」「修完怎么验证」。

## 1. 桌面闪窗：三类，别混

| 现象 | 判据 | 根因 | 修法 |
|---|---|---|---|
| 全屏黑（1920×1080）约 0.4 秒，任务管理器能看到 `msrdc.exe` | 窗口类 `OPContainerClass` / `IHWindowClass`，尺寸等于屏幕分辨率，进程在 `C:\Program Files\WSL\` | WSLg 远程桌面客户端：每次 WSL 虚机启动都 show 一个屏幕大小的窗口 | `dist/wsl2/windows/quiet-wsl-flash.ps1`（`.wslconfig` 里 `guiApplications=false` + `vmIdleTimeout=-1`） |
| 1129×635 的 `Terminal` 黑窗，登录时或**每次部署后**闪一下 | 窗口类 `CASCADIA_HOSTING_WINDOW_CLASS`，`WindowsTerminal.exe` 托管 | 计划任务 `FeishuBridgeBoot` 用 `powershell.exe` 起 launcher，Task Scheduler 为它创建控制台窗口；安装器最后一步 `Start-ScheduledTask` 就是这个触发点 | 任务动作包 `conhost.exe --headless`（安装器 2026-11-03 起自动，`Settings.Hidden` 只隐藏任务条目、不隐藏窗口） |
| 闪一下就没了的小控制台，且 bridge 正在被操作 | 父进程是 `DeepSeek Harness.exe` / `dsh-subprocess-*` | DSH Desktop 自己的 shell 工具，与 bridge 无关 | 停掉那个会话或退出桌面 |

抓 1 秒级窗口不能靠轮询快照，必须事件驱动：`SetWinEventHook(EVENT_OBJECT_SHOW)`（原 `FlashHook.ps1`），
或事件驱动 + 进程审计（`4688`，`auditpol /set /subcategory:{0CCE922B-...} /success:enable`）。
判定「这次是在回收还是有人关机」见 `docs/ops-remote-manage.md` §「虚机为什么总是重启」。

## 2. WSL 虚机反复「重启」：是回收，不是崩溃

判据：`journalctl --list-boots` 每次重启前都是 `systemd-logind: The system will power off now!` →
`Reached target poweroff.target`，`last -x` 有 `shutdown system down` 记录，内核日志无
`panic|Oops|BUG:|general protection`。这是**干净关机**。

两个真因（都已修）：

1. `vmIdleTimeout` 默认 60 秒，没有 `wsl.exe` 客户端就 `poweroff`；holder 15 秒轮询又拉起来 → 循环。
   实测：零客户端 + 默认值 → 100 秒内虚机消失；`vmIdleTimeout=-1` → 100 秒后仍存活。
   **`vmIdleTimeout` 在 WSL 3.0.3.0 上仍然有效**（早期文档说"新版已移除"，是错的，别再照着写）。
2. 安装器每次升级都无条件 `wsl.exe --shutdown`。已改为只在 `/etc/wsl.conf` 真的被改过时才关机
   （`Set-WslConfSystemd` 返回 `already|changed|failed`）。

## 3. 回复链路与队列维护

- 回复走 `events`（claim → `dsh headless` → `reply` → 读回），不直接碰 `inbox`。
- `inbox` / `outbox` 有 `BEFORE DELETE` 触发器（幂等与审计依赖），**不能删行**，只能改状态。
- 过期清理用 `feishu-bridge events prune`：死信 `events` 物理删除；陈旧未完成工作置 `dropped`；
  「回复已确定不发」的置 `ignored`。`--dry-run` 先看、`--include-done` 才删已完成事件。
- `stats.oldest_pending_at` 曾把 `dropped`（作废）算成待处理，导致一次 hold 之后指针永久钉死；
  已排除 `dropped` 与 `outbox=dead/dropped`。

## 3.5 升级了但"跑的还是旧 release"（两个独立的坑）

1. **bridge 服务**：`install.py` 写新 release 和 plist，但进程要重启才换；
   `feishu-bridge restart` 会 bootout+bootstrap，能换过去。用 `status.running_release` 核对。
2. **agentkit 消费者**：`install.py --with-agentkit` **只写 plist、不重载**。若该 consumer 已经在跑，
   `launchctl kickstart -k` 只是按 **launchd 里已加载的旧配置** 重启，进程仍是旧 release 路径。
   正确做法已经是工具：`agentkit-consumerctl.py restart` = bootout + bootstrap + kickstart
   （2026-11-03 前它只做 kickstart —— 那是个真 bug，已改并有回归测试钉住顺序）。
   判据：`ps -o command= -p <pid>` 里的 release 目录是否等于 `status.running_release`；
   **看 plist 文件内容不算验证**。
3. **WSL2 的 DSH 消费者**（2026-11-03 真机发现，第三个坑）：D 托管里重载消费者是
   `sudo bash install-dsh-consumer.sh --restart`，输出被丢进 `/dev/null`。这一步依赖整段安装
   脚本的预检，任何一条不满足就失败——而失败只降级成一行 warning。结果：那台同事机的 bridge 早已
   是新 release，消费者进程还是 3 小时前的 pid，**回复链路一直在跑旧代码**，doctor 却全绿
   （`release_match` 只比 bridge）。根治两件事：重载改成唯一实现
   `dist/wsl2/wsl/reload-consumer.sh`（`sudo -n systemctl restart` + 用 ActiveEnterTimestamp
   前后对比自证，没变就算失败），验收补 `consumer_release` 断言（消费者单元启动时间必须晚于
   `installation.json` 的 `installed_at`，旧了 `verify.sh` 直接 exit 3）。
   判据：`bridge-remote.sh check` 里 `VERIFY consumer_release=ok`；消费者 pid 的启动时间必须晚于
   `installed_at`。**看 unit 文件内容不算验证**（它永远写着 `current/`）。
4. 三个坑的共同点：**看 plist/unit 文件内容不算验证**，要看进程实际 argv 或启动时间。

## 3.6 2026-11-03：拆掉"第二套引擎"和它的所有配套（不是兼容，是删除）

现象：同一件事（领事件→跑 AI→回复→记账防重）有两套实现。旧引擎 `bridge/consumer.py`
（身份写死 `cursor-main`、journal 在 `consumer-journal/`、全局一把锁、只写 `consumer-status.json`）
已经不在生产上跑（线上是 `agentkit-consumer.py`），但它的**入口、installer 的 plist、
`consumer status` 的判定、回滚说法、六份文档**全都还在。结果是每次修问题都要修两遍，
而 `consumer status` 会把"旧引擎停了"读成"没人回复"。

拆除清单（一次删干净，不留"兼容层"）：

| 删掉 | 替换 |
|---|---|
| `bridge/consumer.py`、`cursor-consumer.py`、`tests/test_consumer.py`、`tests/test_cursor_entrypoint.py` | 唯一引擎 `consumer_runtime.py` |
| `feishu-bridge consumer start\|stop\|restart\|foreground`、`consumerctl.LABEL/control/launch_status` | `feishu-bridge consumer status`（只读清单，读 `consumers` 表） |
| `install.py` 写 `io.feishu-agent.cursor-consumer.plist` 的全部代码 | 改为**升级时清理**遗留 plist 与 parked 副本（`_remove_retired_consumer`） |
| `agentkit/rollout.py` 的 `CURSOR_LABEL` / `--take-over` / `stop --restore-cursor` | `start` 幂等 / `restart` 真重载 / `stop` 只停用；回滚走 `feishu-bridge rollback` |
| `consumer-status.json` 参与健康判定 | `live_consumers()` 只看 `state.sqlite3.consumers.last_seen_at` |
| Windows 消费者从**仓库树**跑（`~/feishu-personal-agent/dsh-consumer.py`） | 每个 release 都自带消费者运行时（`install.CONSUMER_FILES`），unit 的 `ExecStart` 指向 `current/dsh-consumer.py`；升级后由 D 阶段自动重启 |

**为什么"从仓库树跑"必须改**：它让两端对"升级后我跑的哪份代码"给出不同答案 —— macOS 的
消费者在 `releases/<id>/`，Windows 的在仓库树，于是同一个"升级完成"在两端含义不同，
排查时还得先问"你这台是哪种"。现在两端都是：release 是唯一可运行副本，`current` 是唯一
切换点，进程重启才换代码。判据统一为 `ps -o args=` 里的 `current`/`releases/<id>`。

同时迁移（不是新写）旧引擎的两个门禁到 `consumer_runtime.runtime_gate()`：`sending_enabled=false`
不领事件、`target_open_id` 启动后被改不领事件；并补上 `hold.json` 挂起中也不领。`config.json`
读不到时消费者启动直接失败（以前是在门禁里静默空转，测试会挂 150s 才暴露）。

教训（值得贴在墙上）：**修好一个东西之后，出问题的那个实现必须删掉**。否则它在未来会以
"另一份文档 / 另一条命令 / 另一套判定"的形式回来，下次连自己都会被搞晕。

## 4. 身份（installer 静默默认 = 别人）

新机器装完以**安装器作者本人**的名字自称，是身份默认值静默继承造成的。安装器现在要求显式
`-Signature`（或当场提问），参数经 `WSLENV` 只透传 `SIGNATURE`。别在文档里再把署名当默认值描述。

**同一机制再犯一次（2026-11-02/03，上一轮只修了署名那半边）**：署名做成了可配置，但
`agent_prompt.py` 与 `bridge/agent.py` 的 **prompt 正文**仍写死「你是<安装器作者>的 AI 助手」，没有配置项。
结果同事那台 Windows 机器对外回复时落款是对的（`来自<该机主人>的AI助手`），**正文却自称是另一个人
（安装器作者）的助手**。判据：同事机发出的回复正文里出现**本机主人以外**的人名。

根因不是措辞，是**拿一个人的名字当身份**。根治 = 中性回退（取不到身份就说「你是这个飞书账号本人的
AI 助手」，署名兜底值同样中性），不是再加一句"记得改名字"的注释。

**同一天又改了一次（第三版，也是最终版）**：第二版把姓名做成了安装期配置 `config.json.owner_name`
（外加 `--owner` / 环境变量 `OWNER` / 从 `lark-cli` 身份派生三条路径）。被指出「身份必须是安装主机
对应的飞书账号，不能是写死或复制的人名」——装完写一份就又是**一份会漂移的人名副本**（换机、换号、
别人接着用都会错）。最终版把配置面整块删除：身份唯一来源是 `status.json` 的 `identity.userName`
（bridge 已用 `target_open_id` 校验过、每轮心跳刷新），DSH/Codex 消费者每次生成回复前现读（不用重启），
内置 analyst 用进程内那份；`install.py` 的 `--owner` / `OWNER` / 派生逻辑全部删除，`config.json` 里
残留的 `owner_name` 也不再被读取（`tests/test_consumer_runtime.py` 有测试专门钉住这点）。

**结论（以后别再犯）**：**身份是账号事实，不是配置项**。问"这台机器的 AI 自称谁"，答案只能由
"这台机器当前登录的飞书账号"给出。机制与判据（`doctor --assert` 的 `reply_identity`）见
[CONTRACTS.md](../CONTRACTS.md)「身份与自称」。

**真机实录（2026-11-03，两台跑同一组判据）**：

- Windows 同事机（WSL2 / Ubuntu）：`bridge-remote.sh check` → `doctor verdict=ok` + `VERIFY RESULT=ok`（19 条全 ok，
  含 `release_match=ok`、`workers=ok`、`heartbeat=2.9s`）；`bridge-remote.sh version` →
  `installed==running==d03c21a07d747834`；`doctor` 的 `reply_identity` = 「自称「<该机主人>的 AI 助手」
  （来源：本机飞书账号身份）/ 署名「来自<该机主人>的AI助手」」；再用**release 里的代码 + 现场 `status.json`**
  渲染 prompt，首句「你是<该机主人>的 AI 助手。」，全文不含另一位同事的名字。
- mac（安装器作者那台）：同一 release `d03c21a07d747834`（从**提交后的 HEAD** 打包，源树重算 id == release 目录名），
  `doctor --assert` exit 0，`reply_identity` = 「自称「<该机主人>的 AI 助手」（来源：本机飞书账号身份）」。
- 两端首次给出**同一个 release id**：此前指纹只覆盖 `bridge/*.py`，同一份代码在两端报不同 id
  （改 `agent_prompt.py` 时 id 甚至不变），"升级了但说不清跑哪份代码"就是这么来的。现在指纹含
  `release.CONSUMER_FILES`（dsh/codex 入口 + consumer_runtime/consumer_safety/agent_prompt），共 16 个文件。
- 顺手清掉那台同事机上 20 个**上游已删除却一直被打进 release** 的残留（旧引擎 `bridge/consumer.py`、
  `bridge/knowledge.py`、`cursor-consumer.py` 及其两个测试、旧评审文档等），备份
  `~/feishu-personal-agent-stale-20261103.tar.gz` 后删除；清理后代码树与发行包 119 个文件逐一对齐
  （`diff` 为空），release 里不再有死文件。
- **端到端补证（同日 19:40，真人 @ 的完整链路）**：管理员账号在只有两人的自测群里 @ 该机主人 →
  采集器 19 s 后抓到（搜索索引滞后约 19 s，仍在 5 分钟的 live 回看窗口内）→ 消费者处理 → 发送并通过
  校验（`outbox.state=reply_verified`），正文自称「<该机主人> 的 AI 助手」、署名「来自<该机主人>的AI助手」。
  判据：`select state, body from outbox where id='<那条消息 id>'`。此前只有进程/验收层证据，"正文"这一环
  是靠这一步补上的；采集器只收 `chat_type=group` + `is_at_me`，DM/自聊都不回。

## 5. 已删除的文件（避免再被引用）

| 删除项 | 原因 | 替代 |
|---|---|---|
| `WINDOWS-PORTING-2026-10-27.md` | 移植评估快照，结论已被拓扑决策取代 | `WINDOWS-DECISION.md`（唯一权威） |
| `WINDOWS-RETRO-2026-10-28.md` / `WINDOWS-FEEDBACK-TEMPLATE.md` | 一次性复盘/反馈模板，且链接到未入库的 `coordination/` | 本文件 §1、§4 |
| `PENDING-RELEASE-20261031.md` | 已发版的一次性清单 | git 历史 |
| `DESIGN-ACK-THEN-FOLLOWUP-20261031.md` | 未实现的 4 人日方案，长期挂在根目录会被误读为已实现 | git 历史（如要做，从这里起重写） |
| `reviews/**` | 带日期的评审快照 | 本文件；`reviews/` 保留为 `remote_doctor --markdown` 的输出目录（已 gitignore） |
| `dist/wsl2/windows/disable-wslg.ps1` | 只关 GUI，不切断空闲回收循环，症状会反复 | `quiet-wsl-flash.ps1` |

## 6. 维护约定（多平台一致性）

- bridge 业务代码**不写平台分支**；平台差异只出现在 `bridge/host.py`（服务管理器）与
  `dist/`（安装器/常驻进程）。macOS = launchd，Linux/WSL2 = systemd（Windows 上必须**系统作用域**）。
- Windows 特有的事实不要写进 macOS 文档：`.wslconfig`、holder、计划任务、`conhost --headless`
  只出现在 `INSTALL-WINDOWS.md` / `dist/wsl2/README.md` / `docs/ops-remote-manage.md`。
- 行为语义（hold/release、事件、回复、配额、幂等）三平台一份，写在 `CONTRACTS.md`。
- 发行包内容 = `git ls-files -co` 的**工作树快照**（已跟踪 + 未忽略的新文件，含未提交补丁），并显式排除
  `archive/`、`reviews/`、`dist/windows/`、`__pycache__`（`dist/wsl2/pack-windows.sh`）。
- 本文「原文在 git 历史里」只对**曾经被跟踪**的文件成立；`CHECKPOINT.md`、`CODE-REVIEW-2026-10-27.md`、
  `CURSOR-SUBSCRIBER-20261027.md`、`FIXES-2026-10-27.md`、`REPAIRS-20261027.md`、`SUBSCRIPTION-2026-10-27.md`
  这 6 个当时在 `.gitignore` 里、从未入库，内容已随本地文件删除而永久丢失（均为一次性笔记，无长期价值）。

## 7. 同事机入网：auth key，而不是「让同事登我的账号」

**以前**：正文写的是「两端登录同一 Tailscale 账号 / 同一 tailnet」，落到操作上就是把管理员本人的
Tailscale 账号密码 / 2FA 交给同事、在同事机器上登录。三个后果都事后才显形：设备归属变成「你的身份」，
事后分不清哪台是谁的；想只收回一台做不到（只能改密码）；同事登出/切号后设备直接离线，人不在机器前就再也连不上。

**现在**：管理员在控制台签一张**一次性 auth key**（Expiration `1 day`、勾 Pre-approved、不勾 Reusable、
单台不勾 Tags），同事只设 `$env:TS_AUTHKEY` 并跑 `tools/windows/setup-remote-debug.ps1 -MacPubKey ...`。
机器进 tailnet 后由管理员在设备页删设备 / 在 Keys 页 revoke。流程与「发给同事的那段话」只定义在
`docs/onboard-windows-dsh-colleague.md` §8.0，其他文档一律链接过去，不复制命令。

**判据**：`tailscale status --json` 里该设备 `Tags` 为空、归属是签发 key 的管理员；同事侧 `BackendState=Running`；
管理员侧 `ssh -o BatchMode=yes <host> 'echo SSH_OK'` 通过。

**踩坑**：脚本在非管理员会话里会自提权（`Start-Process -Verb RunAs`），而提权子进程**不继承**父进程会话里的
临时环境变量（`$env:TS_AUTHKEY` 会丢）。所以脚本必须在提权前把 key 读进变量、再以 `-AuthKey` 参数转发给子进程
（`setup-remote-debug.ps1` 的 `Invoke-Main` 里 `if ($AuthKey) { $arguments += @('-AuthKey', …) }` 已这么做）。漏了这步，同事会被莫名其妙弹到 Tailscale 登录页。

## 8. 前置资源单独装：`-Mode Prereq`（不是第二个安装器）

**以前**：装环境（WSL2/Ubuntu/systemd/apt/Node/lark-cli/OpenSSH）和装 bridge 挤在同一个 `-Mode Install` 里。
新机器第一次失败时，没人分得清是「机器没准备好」还是「bridge/飞书没配好」；同事拿到报错也只能整段重跑。
另有一个 `-Mode Bootstrap` 只做 A 段，没人知道它和 Install 的关系，等于两个同义模式并存。

**现在**：只留 4 个模式 `Install`(默认)/`Prereq`/`Check`/`Uninstall`，`Bootstrap` 删除。
`-Mode Prereq` = A 环境 → B 代码落位（依赖脚本必须从 ext4 跑）→ C 依赖（`setup.sh --deps-only`）→ D Windows OpenSSH → E 体检，
**不装 bridge、不登录飞书、不写 `~/.feishu-personal-agent`**。阶段序列与退出码只在 `dist/wsl2/README.md` §3.2/§3.3.1 定义，
通俗说明在 `docs/windows-prereq-install.md`，发行包新增 `prereq.cmd`。

**为什么不各写一套 apt/npm 逻辑**：PowerShell 里重写一遍 = 第二套装机实现，和 `setup.sh` 迟早各自演化。
Prereq 只负责「按顺序调用 + 逐项断言」，装包逻辑仍只有 `setup.sh --deps-only` 一份。

**判据**：`docs/windows-prereq-install.md` §四的三条命令；或看桌面报告里 E 段的每条 `[ok]`（pid1=systemd、uid≥1000、
python3/sqlite3/curl/git/npm、node≥18、lark-cli、sshd Running、22 端口有 SSH banner）。两条测试钉住顺序与幂等：
`dist/wsl2/tests/InstallWindows.Tests.ps1` 的 Prereq 用例（缺依赖只调一次 `--deps-only`、A→B→C→D→E 顺序）。

**踩坑 1（PS 5.1 编码，2026-11-03 真机）**：`install-windows.ps1` 被编辑器改写成**无 BOM 的 UTF-8** 后，
Windows PowerShell 5.1 按 ANSI(CP936) 解码中文注释 → 吞掉引号 → `ParserError` 83 处，安装器在真机上根本起不来，
而 macOS 的 pwsh 7 完全看不出问题（它默认按 UTF-8 读）。`tools/windows/setup-remote-debug.ps1` 同病。
**根治**：非 ASCII 的 `.ps1` 必须带 UTF-8 BOM；`WindowsDistribution.Tests.ps1` 现在逐字节断言这条，
升级/格式化脚本时别再把这层丢掉。

**踩坑 2（22 端口假阳）**：`TcpClient` 连上 `127.0.0.1:22` 不等于 Windows sshd 在听——WSL 的 localhost 转发
（`wslrelay`）也占着这个端口，WSL 活着时 sshd 挂了照样 "connect 成功"。**判据**：必须读到 `^SSH-` 开头的 banner；
且 `Get-NetTCPConnection` 在非提权会话会「拒绝访问 CIM 服务器」（10s 才失败），`-Mode Check` 里只能用 TcpClient。

**踩坑 3（被调脚本的 exit 语义）**：PS 5.1 里 `& child.ps1` 中的 `exit N` **只结束子脚本**，不终止调用方；
`$LASTEXITCODE` 会被可靠赋值（即使调用前置 `$null`，忘了写 exit 时保持 `$null` 可检出）。
所以不需要为了拿退出码再开一个 `powershell.exe` 子进程，也别把原因写成「`$LASTEXITCODE` 不可靠」。

**实测记录（2026-11-03，真实 Windows 11 build 26100 / Windows PowerShell 5.1.26100 / Ubuntu 24.04 WSL2）**：
- 三个 .ps1 `Parser::ParseFile` 全 `PARSE=OK`，首字节 `efbbbf`（补 BOM 前 `install-windows.ps1` 是 `PARSE=FAIL errors=83`，line 230 UnexpectedToken）。
- `WindowsDistribution.Tests.ps1` exit=0 / ok=147 / FAIL=0；`InstallWindows.Tests.ps1` exit=0 / ok=45 / FAIL=0（Windows 上 10 条 POSIX 伪 wsl 断言按设计 SKIP）。
- 真机直达新阶段（跳过 A/B，未覆盖活树）：`Install-PhasePrereqDeps`=0（依赖已齐整段跳过）、`Install-PhaseOpenSsh`=0（sshd 已在跑跳过）、
  `Install-PhasePrereqCheck`=0，输出含 `pid1=systemd uid=1000 node=v22.23.4 lark-cli version 1.0.98`、
  `Windows 22 端口读到 SSH banner（是 Windows sshd，不是 WSL 转发）`；`Test-SshPortOpen=True` / 已删的 `Test-TcpPortOpen=False`。
- 判据重放：`powershell -NoProfile -ExecutionPolicy Bypass -File dist\wsl2\tests\WindowsDistribution.Tests.ps1` 与 `...\InstallWindows.Tests.ps1`；macOS 用 `pwsh -File` 同样两套。

**踩坑 4（PS 5.1 的 stderr = 致命错误，2026-11-03 同事机现场）**：新机器第一次跑 `prereq.cmd`，输出
`=== A 环境 ===` → `[ok] Windows build 26100` → 裸 `NativeCommandError`（`install-windows.ps1:406`）→ 退出码 1，
没有任何可读原因。根因不是 WSL 装不上，是 `& wsl.exe --status *> $null`：PS 5.1 在
`$ErrorActionPreference='Stop'` 下把原生命令写到 stderr 的每一行变成 **终止性** 的 `NativeCommandError`，
而 `wsl --status` 恰好把「没有发行版，运行 wsl --install」写在 stderr 上（中文还会按 GBK 解码成乱码）。
根治：**所有** `wsl.exe`/`ubuntu.exe` 调用走 `Invoke-Wsl` → `Invoke-Native`（调用期间把 EAP 放宽成 `Continue`，
只按退出码判断）；wsl 安装失败/内核读不到改为 `Write-Err + return 2`，不再 `throw`；顶层入口加 try/catch，
意外异常也打印「安装器意外中止：原因」+ 写桌面报告 + 退出码 1。**判据**：`InstallWindows.Tests.ps1` 断言
安装器里裸 `& wsl.exe` / `& ubuntu.exe` 为 0 处、`Invoke-Native` 在「命令写 stderr + 退出 3」时返回 Code=3 且把
stderr 当数据。真机复现链（同形假体 `choice.exe` 改名 `wsl.exe`，任何参数都写 stderr + 退出 255）：
旧版 `1bf7ec43` → `THROWN_FQID=NativeCommandError`、函数未返回；新版 `c18d44fb` → 打印自己的中文提示、`PHASE_CODE=2`、无异常逃逸。
（`cmd.exe` 当假体复现不出来：它写 stderr 但退出 0。）
**重启分支的真机证据（同一轮，假体用 `csc.exe` 编译成真 exe —— .bat 改名 `wsl.exe` 会被 CreateProcess 拒成
`ERROR_BAD_EXE_FORMAT`）**：假 `--status` 写 stderr + 退出 255，假 `--install` 退出码可配；结果
`0 / 3010 / 1641 → PHASE_CODE=3010`（两行中文提示正常打印，`Test-RebootRequiredCode` 判对），
`255 → PHASE_CODE=2`（失败分支）。证明 3010 是**判出来的**，不是写死的。

**踩坑 5（stderr 混进解析数据）**：`Get-WslDistros` 用 `2>&1` 取 `wsl -l -q`，WSL 的告警会被当成发行版名
（真机见过失败提示里出现 `现有：The system cannot find message text…`）。根治：解析型调用加 `-DropStderr`
（只取 stdout），发行版名解析抽成纯函数 `ConvertFrom-WslDistroList` 并用单测钉住 UTF-16LE(NUL) 输入。

### 8.1 离线载荷：给「断网 / 不想依赖网络」的新机器（2026-11-04）

**需求**：给另一台全新 Windows 装前置环境，包放 NAS，同事自己双击脚本执行。要求「离线优先、缺项才联网」，
且必须准确（不能出现"照着做却装不上"）。

**五个决定**（都写进 `reviews/win-prereq-offline-DESIGN.md`，实现只此一份）：

1. **载荷是安装器的一个来源维度**（`-PayloadDir`），**不写第二个安装器**；`-PayloadDir` 留空 ⇒ 行为与原先逐字节相同。
2. **离线发行版走 `wsl --import`**（不是 `wsl --install --from-file`）：后者要求 WSL ≥2.4.4 且必须交互过 OOBE，
   用户名/密码不可控；而新机器恰恰可能连 `wsl.exe` 都没有。`--import` 是「脚本给什么就是什么」。
3. **新增 `-Mode PrereqCheck`**（只读、不提权，exit 0/7/2），给「02-验收」复用 E 段断言 —— 验收不是第二套判断。
4. **`wsl --shutdown` 只留给纯在线路径**；离线分支一律 `wsl --terminate <Distro>`。理由：参照机上有生产
   `Ubuntu` + bridge holder，整机关机会把同事在用的东西一起关掉。测试钉住「全局 `--shutdown` 全仓只有一处」。
5. **WSL MSI 用 3.0.3.0（stable）**：3.0.4/3.0.5 在上游都是 `prerelease=true`，而参照机实测运行的就是 3.0.3.0。
   给别人的机器做兜底不押 prerelease。换版后 digest / Authenticode / MSI 数据库三方重核（`payload/PROVENANCE.md` §7.0）。

**包的形状**（`dist/wsl2/pack-offline.sh` 生成；NAS 上永远不手改）：根目录三个编号 `.cmd`（00 自检 / 01 安装 / 02 验收）
+ `scripts/`（4 个 ps1，`.cmd` 只做 UNC 拒绝 + 自提权 + 调它们）+ `repo/`（仓库快照）+ `payload/`（10 个文件，775 MB）
+ `README.md` + `VERSION.txt` + `MANIFEST.sha256`。**故意删掉** `install.cmd`/`check.cmd`/`prereq.cmd`：
一个包只给一个装法，避免同事顺手把 bridge 也装了（那台机器只该变成「准备好的空机器」）。

**踩坑 1（数组 splat 是位置传参）**：`& $Installer @('-Mode','Prereq',…)` 在 PS 5.1 里不是具名传参，而是按位置绑定 →
`Cannot validate argument on parameter 'Mode'. The argument "-Mode" does not belong to the set …`，包装脚本 exit 1，
**安装器一行都没跑**。具名只能哈希表：`@{ Mode = 'Prereq'; … }`。

**踩坑 2（stderr 被渲染成红块）**：`& wsl.exe … 2>&1` 收来的 stderr 是 `ErrorRecord`，`Out-String`/`Write-Host`
会把它渲染成「所在位置 / CategoryInfo / NativeCommandError」一大块（控制台里是红的）。`setup.sh` 第 2 步的
`WARN: npm 还不在 PATH 里` 就这么变成红块，紧接着三步其实全装成功了 —— 同事只会看到"红字报错"。
根治：在 `Invoke-Native` 的**捕获处**把 `ErrorRecord` 降级成 `$_.Exception.Message`（内容一个字不少，PowerShell
的错误外壳全丢）；`install-openssh.ps1` 里 `install-sshd.ps1` 的输出同样处理；`preflight.ps1` 的 `Invoke-Capture` 同理。

**踩坑 3（我自己的错，记下来免得再犯）**：一度认定「被调脚本的 `exit` 会连调用方一起结束」，于是给两个包装脚本
加了 `powershell -File` 子进程 + 子进程输出还得 `| Out-Host` 否则变成函数返回值……**全部是多余的**。
本文件「踩坑 3」早就写明：`& script.ps1` 里的 `exit` 只结束那个脚本，`$LASTEXITCODE` 会被可靠赋值。
2026-11-04 真机反证：`a.ps1` 里 `& b.ps1`（`b.ps1` 结尾 `exit 7`），`a.ps1` 里紧跟着打印的
`[a] AFTER-CALL last-exit=7` 正常出现、外层 driver 也继续跑。子进程实现已删除，回到同进程调用。

**踩坑 4（`dpkg -s` 不能当"装没装"的判据）**：离线路径里 npm 由 Node 官方 tarball 带进 `/usr/local/bin`，
`dpkg` 永远不认识它 → `dpkg -s npm` 判成「缺」，会让安装器在下一阶段把它当成没装好。判据统一为 `command -v`
（E 阶段本来就是 `command -v`，同一事实只能有一个判据）。

**踩坑 5（双引号里 `$name:` 会被当作用域变量）**：`"C:\WSL\$distro:"` 之类被解析成 scope 变量 →
`ParserError: 变量引用无效`。写 `${distro}:`。

**踩坑 6（UNC 上既不能提权也不能放 vhdx）**：从 `\\NAS\…` 双击 `.cmd` 时 `Start-Process -Verb RunAs` 会失败，
`wsl --import` 也不该把 ext4.vhdx 建在网络盘 → `.cmd` 一开头就拒 UNC 并把整包指到本机磁盘（README 第一节就是"先拷到本地盘"），
`-DistroRoot` 同样拒绝 UNC/相对路径（exit 2）。

**踩坑 7（UAC 提权会丢掉命令行参数）**：`.cmd` 自提权走 `Start-Process -Verb RunAs`，参数不会过去。所以三个 `.cmd`
**只接受零参数**、带参数一律报错并指向 `scripts\*.ps1`；README 里写明"带参数请在管理员 PowerShell 里直接跑 ps1"。

**踩坑 8（PROVENANCE 里手抄 GUID）**：OpenSSH MSI 的 `UpgradeCode` 手抄错了一个字符（末组 13 位，根本不是合法 GUID）。
GUID / 哈希这类字串只能从数据库或文件**直接复制**，不要手抄；已被独立验证者抓出并更正（`payload/PROVENANCE.md` §6 顶部有更正记录）。

**踩坑 9（`return @()` 把空数组展开成"没有输出"）**（2026-11-07 全新真机踩到）：一个发行版都没装的机器上
`wsl -l -q` 返回 exit 0 + 空输出；`ConvertFrom-WslDistroList` 用 `return @(...)` 交回空数组，PowerShell 把它展开成
"没有任何输出"，上层赋值拿到 `$null`，A 阶段据此打印「WSL 尚未初始化完成：请重启 Windows 后重跑」—— 而重启多少次
都不会变。修法：`Get-WslDistros` 用 `,@()` 双保险（内层 `@()` 把空结果变成空数组，外层 `,` 挡住 return 的展开），
并把三态语义写清（`$null` = wsl 调用真失败；`@()` = 调得通但没有发行版；否则是名字数组），测试钉住「空列表不许
变 `$null`」。判据：真机 `Get-WslDistros` → `ISNULL=False COUNT=0 TYPE=Object[]`，随后 A 阶段正常 `wsl --import`。

**踩坑 10（`wsl.exe` 的命令行还要再过一层解析）**：把 bash 脚本塞进 `wsl … -- bash -lc '<script>'` 时，脚本里的
`$(…)` / `$var` / `;` 会在到达 bash 之前就被展开、拆散（真机现象：`bash: -c: line 1: syntax error near unexpected
token ';'`，命令里的 `$f` 已经变成空串）。`Invoke-WslBash` 那套 base64 之所以没出事，是因为 b64 里没有 shell 元字符。
需要「脚本 + stdin 同时用」的场合（密码走 stdin），改为把脚本写成 Windows 临时文件、用 `/mnt` 路径执行，命令行上
不留任何要转义的字符。

**踩坑 11（sudo 时间戳按 tty 记，跨 `wsl` 调用不继承）**：先单独跑一次 `sudo -S -v` 预热，再另起一次 `wsl` 跑
`setup.sh`，里面的 sudo 照样要密码（实测第二次 `sudo -n true` → `sudo: a password is required`）。预热必须和
`setup.sh` 在**同一次** bash 会话里完成：脚本第一行 `read -r SUDO_PW` 从 stdin 吃掉密码 → `sudo -S -v` → 继续执行。
密码不进 argv、不进环境变量、不落盘（`-LinuxPassword` 的无人值守路径就是这样跑通的）。

**判据（可重放）**：
- 代码层：`python3 -m pytest -q`（726 passed）+ 参照机 `[Parser]::ParseFile` 全 `.ps1`（errors=0）+
  `dist\wsl2\tests\WindowsDistribution.Tests.ps1`（185 ok）/ `InstallWindows.Tests.ps1`（110 ok）。
- 包层：`shasum -a 256 -c payload/SHA256SUMS`（10/10）+ 根目录 `MANIFEST.sha256`（全量核，Mac 与 Windows 双端 0 不符）。
- 行为层（参照机隔离发行版 `UbuntuOfflineTest`，从零 `wsl --import`）：`00` → `PREFLIGHT ok=17 warn=1 bad=0` 且
  `WSL MSI 在位：wsl.3.0.3.0.x64.msi`；`01` → `DEPS source=payload step=apt|node|lark-cli`（**无** `source=online`）、
  `PREREQ_DONE … exit=0`，WSL 内 `pid1=systemd uid=1000 node=v22.23.4 lark-cli version 1.0.98`；`02` → `VERIFY … exit=0`；
  第二次运行 01 全跳过、exit 0；全程生产 `Ubuntu` 的 bridge pid 未变（377379），测完 `wsl --unregister` + 删目录。

**未覆盖（如实记）**：`msiexec /i` 两个 MSI 都没有真跑（会改参照机的 WSL/sshd 版本）；UAC 自提升与非管理员双击未覆盖。

**2026-11-07 补覆盖**：拿到一台全新的物理机（Windows 11 家庭版中文版 26200 / WSL 3.0.3.0 / 一个发行版都没有），
用 SSH 全程无人值守跑通 `-Mode Prereq`：A 阶段 `wsl --import` + **新参数 `-LinuxPassword` 自动建用户**（不再停下来
要人输密码）→ B 镜像代码 → C 从离线载荷装 apt/Node/lark-cli（sudo 靠 stdin 预授权，不弹 WSL 窗口）→ D `-SkipOpenSsh`
跳过 → E 体检 `pid1=systemd uid=1000 node=v22.23.4 lark-cli version 1.0.98`，`PREREQ exit=0`。也就是说原先「A 阶段人输
密码」「C 阶段 sudo 窗口」两个只能靠测试替身模拟的缺口，现在有真机判据了。

#### 踩坑 12（2026-11-07，setup.exe 的 DSH 步骤）：`pipefail` + `grep -q` = 假失败

`set -euo pipefail` 下写 `echo "$BIG" | grep -q "$PATTERN"` 作为断言：`grep -q` 一匹配上就退出，
上游 `echo` 收到 SIGPIPE，管道整体状态变成 **141**，于是「匹配成功」被当成「没找到」。
表现是同一份 dump 有时过有时不过（取决于 grep 在第几行命中），最容易怀疑到「配置没写进去」。
修法：用 bash 自己的模式匹配 `[[ $BIG == *"$PATTERN"* ]]`，不起子进程、没有 SIGPIPE。

#### 踩坑 13（2026-11-07）：`su` 在「stdin 已读完的管道」里会失败

安装器的 API key 是从 stdin 喂进 `install-dsh.sh` 的（不进命令行、不进环境变量）。脚本里再调
`su - <user> -c 'dsh --profile web --dump-config'` 时，stdin 已经在 EOF，`su` 拿不到会话，
命令静默返回空 —— 断言于是报「dump 里没有 provider」。
修法：`su … </dev/null`。凡是「从 stdin 收密钥、随后又切换用户」的脚本都会踩到。

#### 踩坑 14（2026-11-07）：PS 5.1 抓服务端证书链，只有 HttpWebRequest 那条路能走

两条死路，都在真机（Win11 26200 / PS 5.1）实测：
1. `SslStream` 的 `RemoteCertificateValidationCallback`：回调能进，但传进来的 `X509Chain`
   `ChainElements` 恒为 **0**（握手期间还没填），`$certificate` 包成 `X509Certificate2` 后
   `Subject` 也没了；
2. 换成 `HttpWebRequest` + `ServicePointManager.ServerCertificateValidationCallback` 能拿到填好的链
   （实测叶+根 2 张），但**必须在回调内部就把证书取出来**：同一个 `X509Chain` 实例在握手结束后
   会被清空（回调内 `ChainElements=2`，回调外再读=0）。

另外 `& wsl.exe … 2>&1` 收 WSL 输出时，`update-ca-certificates` 的 rehash 告警会被 PS 当成
`NativeCommandError` 抛出来，若 `$ErrorActionPreference='Stop'` 会直接终止脚本。自己收 stdio。


## 9. 未收完的账（每条都带到期日或明确的决定点）

> 按 AGENTS §0 第 5 条：不允许"两套实现并行"长期存在，也不允许用"先兼容一段时间"糊过去。
> 要么当天删完，要么在这里开一条**带到期日**的任务。到期没做的，下一次改动先做它。
>
> 已经删干净的一条：sshd 安装曾有两份实现（`setup-remote-debug.ps1` 自带一份、`install-openssh.ps1`
> 一份，收敛策略还不一样）。上一版在这里开了到期日，随即发现"单文件交付"这个理由并不成立 ——
> 同事拿到的是含 `repo/` 的整包，`setup-remote-debug.ps1` 旁边的 `install-openssh.ps1` 一直都在，
> 于是当场删掉前者那份、改为调用后者（判据：`grep -c "function Install-SshdServer" tools/windows/*.ps1` 只有 1）。
>
> 合并后在**真机**（Windows 11 / PS 5.1）上跑委派链路，暴露了三个只有真跑才会出现的问题，都已修：
>
> 1. **防火墙判据误伤无关规则**：先把一批规则的 `Get-NetFirewallPortFilter` 收集成数组、再按规则下标
>    回配 —— 有些规则没有端口过滤项，数组短一截、下标整体错位，结果把两条 `HNS Container Networking`
>    的 53 端口规则停用了（本该动 22 的规则反而没动）。改为**逐条规则问它自己的端口过滤项**，判据抽成
>    纯函数 `Test-FirewallRuleCoversPort`（只看协议+端口，不按名字猜，端口支持范围和逗号分隔），
>    并加静态断言禁止回到"按下标配回"的写法。现场已恢复（两条 HNS 规则重新启用）。
> 2. **`& script` 的返回值不是退出码**：`$code = & $installer @args` 拿到的是脚本输出，判 `-ne 0` 永远成立
>    → 明明装好了却报"退出码为空"。改成先 `& $installer @args` 再读 `$LASTEXITCODE`。
> 3. **`-SshPort` 默认 0 被当成端口传下去**：`Get-SshPort` 的语义是"0 = 读 sshd_config / 22"，直接透传
>    给唯一实现就变成 `Port 0`，自检打印 `listen(127.0.0.1:0) = False`、退出 1。改为先解析端口再委派。
>
> 复跑证据：`setup-remote-debug.ps1 -SkipTailscale -MacPubKey <现有公钥>` → `delegate_exit=0`、
> 报告里 `"listening": true`、公钥 `already`（不重复追加）；两套 `.ps1` 测试在真机上全绿。

### 9.0 答不上来时没人知道（2026-11-04 已修：私聊求助 + 漏答提醒）

同事反馈原话：「bridge agent 遇到答不上来的时候，应该在私人通道里问本人；同时应该更主动去查资料」。
当时的实际语义是：Agent 只有一条出口（对当前会话的回复），答不上来就 `fail`，事件在队列里重试到死——
**本人永远不知道有人在等他**。一位同事那台机器 2026-11-04 17:56 的突发（15 条 @，10 条 `InsufficientTime`）
就是这个形态：没有任何一条通知到本人。

改法（一条事实一个入口，不新增第二条发送语义）：

- **Agent 声明式求助**：答复 JSON 可带 `{"ask_owner":{"question":…, "needs":"fact|decision|access"}}`
  （`consumer_safety.finalize_reply` / `extract_escalation`）。运行时在**回复提交成功之后**用新的
  顶层命令 `feishu-bridge notify` 私聊本人；求助失败不影响已发出的回复。
- **漏答提醒**：运行时自己放弃事件（`InsufficientTime` / `AgentFailed`）时发 `fail:<id>:<reason>`。
- **不刷屏**：`bridge/notify.py` 同键 6 小时去重 + 每小时上限（`notify_hourly_cap`，默认 6）；
  `notify` 的正文只从 stdin 读、附带 `signature`、状态在 `home/notify.json`。
- **prompt**：`agent_prompt` 增加主动查证的顺序（上下文 → 工作区 → 本机命令 → 上网）与一条可自查的
  求助判据（"正文一旦把决定推给本人，就必须带 ask_owner"）。

真机暴露并当场修掉的四个问题（都是只有跑起来才看得见的）：

1. **适配器把 `ask_owner` 剥掉了**：`dsh-consumer.py` / `codex-consumer.py` 先 `extract_answer()` 再返回，
   运行时只拿到正文 → 求助永不触发。改为适配器只校验、返回 final 原文，解析/脱敏/限长只在
   `finalize_reply` 一处（`agentkit/adapters/legacy.py` 的副本同步改成调用 `finalize_reply`）。
2. **JSON 字符串里的裸换行**：模型把多行答复写成一行 JSON 却用真实换行 → 整条被判"不是 JSON"→ 生成失败。
   `_repair_control_chars` 在字符串内部把控制字符转义。
3. **JSON 字符串里的未转义双引号**（例如 `超出"看"的范围`）：同上，`_repair_unescaped_quotes` 按"引号只在
   `{[,:` 之后开、只在 `,}]:` 之前关"的规则修；实在修不好时 `_salvage_reply` 只在"整段就是一个
   `{"answer":…}` 对象"时抢救字段——引文夹带对象、括号不配对、超长回复仍然失败关闭（原有安全测试不变）。
4. **LaunchAgent 被自己的安装器删掉**：consumer 的 label 曾用 `io.feishu-agent.dsh-consumer`，
   而 `install.py::_remove_legacy_services()` 的判据是"文件名/label 里有 feishu 且不在 `CURRENT_LABELS`"，
   于是每次安装都把它当旧版本清掉（实测两次）。当时的临时处置是不带 `feishu` 的 label；
   **已根治**：label 回到 `io.feishu-agent.dsh-consumer` 并由安装器管理，归属判据改成"argv 指向本机 home"，
   见 §9.0b 与 §9.0c。

真机验收（本机 macOS，release `c0940a1d48565a80`）：

- `echo … | feishu-bridge notify --dry-run` → `{"ok": true, "dry_run": true, "decision": "send"}`
- 真发一条 → `{"ok": true, "sent": true, "message_id": "om_…", "chat_id": "oc_…"}`（具体 id 见本机 `coordination/`）；
  同一正文再发 → `decision: "duplicate"`
- 群里 @ 本人一条"请你拍板"的消息（event 102）→ attempt 记录 `ask_owner` 非空、`escalated.ok=true`，
  私聊收到一条 `om_…`，正文以「【需要你出手】…」开头并带署名
- 故意让一次生成失败（event 101 第一次，坏 JSON）→ `fail:101:AgentFailed` 私聊提醒到达
- `python3 -m pytest -q` = 770 passed（含 `test_notify.py`、`test_consumer_escalation.py` 与新增的
  `RawControlCharacterTests` / `BrokenJsonRepairTests` / `EscalationContractTests`）

### 9.0b macOS 的 DSH consumer 没有被安装器管理（2026-11-04 已根治）

当时的现状：`install.py` 只写 bridge / agentkit-consumer / attention-watch 三个 LaunchAgent，
`CURRENT_LABELS` 也只认这三个 label；DSH consumer 的 plist 是本机手工创建并 `launchctl bootstrap` 的，
所以 `_remove_legacy_services()` 会把它当旧版本删掉（label 里带 feishu 却不在白名单），一天内删了两次。
临时处置是换一个不带 `feishu` 的 label —— 那只是让它**躲过**清理，并没有回答"谁负责这个进程"。

根治（本次）：选方案 1，安装器直接管理它。

- `install.py --with-dsh-consumer`：label 进 `CURRENT_LABELS`、由安装器写 plist（入口 `current/dsh-consumer.py`、
  PATH 显式带安装器选中的 `dsh`、默认 consumer 名 `dsh-main`、默认 workspace `~/dsh-workspace`），
  写盘后 **bootout+bootstrap** 重载并等 `consumers.last_seen_at` 变新鲜才算 `live`；
- 归属判据从"文件名/label 猜"换成 **argv 是否落在本机 home 下**（`_plist_owned_by_bridge`）：
  手工换过 label 的单元也能被正确接走，不再靠名字像不像；
- 不传 `--with-dsh-consumer` 的升级也会**重载已有消费者**（`_reload_existing_dsh_consumer`）：
  "升级"必须让 bridge 和消费者同时换 release，不能靠记得加参数；
- 判据：`launchctl print gui/$(id -u)/io.feishu-agent.dsh-consumer` 的 `pid` 与
  `ps -o args= -p <pid>` 里的 `current/dsh-consumer.py`，加 `feishu-bridge consumer status` 的
  `dsh-main live=true`。真机实录见 §9.0c。

### 9.0c 升级以后服务没起来 / 消费者跑旧代码（2026-11-04 已根治）

同事的问题原话：「为什么我的 bridge 今天升级以后，服务没有起来」。根因不是升级脚本，是
**安装的语义少了一步**：`install.py` 只写 plist（build + write），激活一直留给操作者手工
`feishu-bridge start`。11:50 那次升级顺带 `_remove_legacy_services()` 把还在运行的旧 label
bootout 掉，于是磁盘上装着新 release、launchd 里 `loaded:false`、`status.json` 心跳停在 8 小时前 ——
"升级成功"和"服务在跑"被当成了同一件事，而它们从来没有在同一处被断言过。

根治：**安装 = 构建 + 写入 + 激活**（与 Windows 安装器 A–F 阶段早就有的语义对齐）。

- `_activate()`：`systemd-system`（WSL2 系统级）返回 `attempted:false` 并指名
  `finalize-systemd.sh` 的 D 阶段接管；macOS / systemd-user 走 `bridge.cli restart` 并**等到
  `status.json` 的 `running_release` 等于新 release 且心跳新鲜**，回执写进 `installation.json` 的
  `activation`；失败退出码 **3**（与 Windows「3 systemd 未就绪」同一含义），不静默降级；
- 激活放在释放 `install.lock`/`cli.lock` **之后**（CLI restart 自己也要 `cli.lock`，进程内会死锁）；
- `install()` 默认 `activate=False`，只有 CLI `main()` 传 `True`：pytest 永远不会 bootout 生产单元
  （这次已经把"测试踩到真机"作为一个真实事故记在这里：修的过程中一次 pytest 删过本机 consumer 的 plist）。

同一次全量审计还查出 Windows 侧两个"看起来成功、其实没生效"的点，一并根治：

1. **消费者单元名两套写法**：安装器写 `io.feishu-agent-dsh-consumer.service`（连字符），
   而 `finalize-systemd.sh` / `reload-consumer.sh` / `verify.sh` / `enable-reply.sh` 全用
   `io.feishu-agent.dsh-consumer.service`（点号）。后果不是报错而是**静默跳过**：升级时
   `reload-consumer.sh` 判定"消费者未安装"→ exit 0，消费者留在旧 release，`verify.sh` 的
   `consumer_release=not-installed` 又被当成过。现在：统一点号；D 阶段对"只有连字符旧单元"的机器
   做一次性迁移（迁移失败即 fail，不再吞成 warning）；`reload-consumer.sh` 见到旧名单元时明确
   报 `RELOAD_CONSUMER=legacy-unit-name` 且 exit 1，不再谎报"未安装"；
   `tests/test_windows_consumer_unit.py` 新增门禁：安装器展开后的单元名必须与其余四个脚本逐字一致。
2. **`dist/wsl2/setup.sh` 不看 `install.py` 的退出码**（审计 R6）：install 失败照样往下走，最后
   报"完成"。现在 0 继续、3 说明"D 阶段会真正启动"、其它非 0 立即失败。

**真机实录（本机 macOS，正文 `6ad0db440f34dee0`）**：

- 带消费者升级：`python3 install.py --with-dsh-consumer --dsh-workspace <home>/Documents` →
  退出码 0，`activation.bridge={ok:true, pid:45154, release:"6ad0db44…", was_loaded:true}`，
  `activation.dsh_consumer={label:"io.feishu-agent.dsh-consumer", reloaded:true, pid:45215, live:true}`
- 不带消费者参数再升一次（验证"升级也重载已有消费者"）：`activation.dsh_consumer.created=false,
  reloaded=true, live=true, pid:45750`
- 机器状态：`launchctl print gui/$(id -u)/io.feishu-agent.bridge` → `state = running`；
  `ps -o args= -p 45154` → `<home>/bin/feishu-bridge …`；`ps -o args= -p 45750` →
  `<home>/current/dsh-consumer.py --consumer dsh-main --kind dsh --home … --workspace <home>/Documents`；
  `feishu-bridge status` → `running_release==installed_release==6ad0db44…`、`update_pending=false`、
  `receiving_healthy=true`；`feishu-bridge consumer status` → `dsh-main live=true`、`healthy=true`；
  `feishu-bridge doctor --assert` → `verdict: ok`
- 旧的手工 plist（带个人名字的那个 label）已被 `_remove_legacy_services()` 按 argv 判据删掉，
  `~/Library/LaunchAgents` 里只剩 `io.feishu-agent.dsh-consumer.plist`；`pgrep -fl dsh-consumer.py` 只有 1 个进程

同一晚在另一台宿主（Windows/WSL2）上顺带验证了**统一的丢弃计数口径**：`hold` → `release` 的回执说
「挂起期间丢弃 20 条消息」，账目对得上 —— `messages=10`（17:56 那批未答的 inbox 行）+
`notices=10`（同名 attention 事件），落地是 `inbox dropped 24→34`、`events dead 10→20`，
`status` 的 `inbox_needs_attention` 由 10 变 0。旧实现会把同一批数成 30（inbox+outbox+events 行数相加）。

### 9.0d 远程升级连跑三次都"成功"、机器上什么都没变（2026-11-04 已根治）

给 Windows 那台机器发版时，`tools/bridge-remote.sh upgrade` 连续三次都以**容易误读的方式**结束
（第一次 `| tail` 把退出码吃成 0，后两次是自己的 PowerShell 脚本报参数为 null），机器上
`release_id` 与单元一个字都没变。三层原因叠在一起，任何一层单独看都像"小问题"：

1. **打包不给非 ASCII 文件名置 UTF-8 标志位**：`zip -qr`（Info-ZIP）在 macOS 上写的
   `02-验收（不需要管理员运行）.cmd` 这类文件名没有 general purpose bit 11，Windows 的
   `bsdtar` 只能按 CP936 猜 → `Invalid empty pathname`，解压整包失败。
2. **换 `Expand-Archive` 只是把失败换成噪声**：它的 `-Force` 是"合并覆盖"，模块内部清理已展开项时
   抛 "找不到路径 …prereq.cmd"，输出里真正的失败被这句废噪声盖住。
3. **生成的临时 `.ps1` 没有 UTF-8 BOM**：Windows PowerShell 5.1 对无 BOM 文件按 ANSI 解码，
   中文注释的字节错位把**下一行代码并进注释** —— `$tar = Join-Path …` 整行消失，
   脚本报 `Test-Path 参数为 null`，而退出码被外包管道吞掉。

根治：

- 打包只有一份实现 `dist/wsl2/make-zip.py`（`zipfile.ZipInfo.from_file` 会置 UTF-8 标志位，
  并 `ZIP_DEFLATED`），`pack-windows.sh` / `pack-offline.sh` 都调它，不再用 `zip -qr`；
- 升级侧用 Windows 自带 `System32\tar.exe` 解包（解不出即 `TAR=failed` 且删残包），
  残包缺 `install-windows.ps1` 就 `INSTALLER=missing` 退出；
- 写临时 `.ps1` 前先落 UTF-8 BOM；`bridge-remote.sh` 的调用方不要再用 `| tail` 取退出码；
- `tests/test_pack_windows.py` 把这三条钉成门禁（含"非 ASCII 文件名必须有 bit 11"的真跑断言）。

真机结果（2026-11-04 22:22，同事的 Windows/WSL2 主机）：一次升级跑通，父目录下两个
带人名拼音的旧单元（`com.<人名>.feishu-personal-agent.service` /
`com.<人名>.feishu-dsh-consumer.service`）被 D 阶段的迁移清掉（`MIGRATE clean_count=4`，
含 `multi-user.target.wants` 里的 enable 链接），`io.feishu-agent.bridge.service` 与
`io.feishu-agent.dsh-consumer.service` 建立并 active；消费者由
`signin:<home>/.dsh/.credentials.yaml` 起，`ActiveEnterTimestamp` 22:22:11 → 22:22:17（自证重载），
`version` 里 `installed_release==running_release==6ad0db440f34dee0`，F 阶段逐项 ok
（`legacy_units=0`、`consumer_release=ok`）。

顺带根掉的假绿：这台机器此前 `consumer_release=ok` 是因为 verify 只找 canonical 单元、
找不到就 `not-installed` 通过 —— 实际消费者一直跑在带人名的旧单元上。现在
`verify.sh` 会在"canonical 单元缺失但 consumers 表 5 分钟内有活消费者"时判
`consumer_release=unmanaged`（exit 3）。

### 9.1 systemd unit / LaunchAgent 名里带作者姓名拼音（2026-11-04 已改名并迁移）### 9.1 systemd unit / LaunchAgent 名里带作者姓名拼音（2026-11-04 已改名并迁移）

原名字形如 `com.<作者拼音>.feishu-personal-agent`、`com.<作者拼音>.feishu-dsh-consumer`、
`com.<作者拼音>.feishu-agentkit-consumer`、`com.<作者拼音>.feishu-attention-watch`。
这是**把某个人的身份写进系统命名**：换人装机后 unit 名还在替上一个人做广告，与"身份只能来自
本机登录账号"的原则直接冲突，也是仓库里最后一类个人信息。

改名方案（一次到位，不留兼容开关）：

| 旧 | 新 |
|---|---|
| `com.<作者拼音>.feishu-personal-agent` | `io.feishu-agent.bridge` |
| `com.<作者拼音>.feishu-dsh-consumer` | `io.feishu-agent.dsh-consumer` |
| `com.<作者拼音>.feishu-agentkit-consumer` | `io.feishu-agent.agentkit-consumer` |
| `com.<作者拼音>.feishu-attention-watch` | `io.feishu-agent.attention-watch` |
| `com.<作者拼音>.feishu-cursor-consumer`（已退役） | `io.feishu-agent.cursor-consumer`（仅用于清理） |

**迁移**（改名必须带迁移，否则旧 unit 还在跑、还 enabled，会和新 unit 抢同一个 home =
双采集 / 重复回复，而这正是 §7 记录过的事故）：

- macOS：`install.py` 的 `_remove_legacy_services()` —— 扫 `~/Library/LaunchAgents` 与
  `disabled-consumers/`，凡 label 里带 `feishu` 且不是当前三个 label 的，`launchctl bootout` + 删。
- WSL2/Linux：`dist/wsl2/wsl/migrate-legacy-units.sh` —— 凡是名字里带 `feishu` 且不以
  `io.feishu-agent.` 开头的 unit/timer/wants 链接，`disable --now` + 删 + `daemon-reload`；
  由 `finalize-systemd.sh` 与 `install-dsh-consumer.sh` 在装新 unit 前调用。
- 验收：`dist/wsl2/wsl/verify.sh` 增加 `legacy_units=0` 断言（非 0 直接判失败）。

判据**不是**"列一张旧名字清单"——那等于把姓名拼音重新写回仓库。两条脚本都按**形态**识别
（带 `feishu` + 不是新前缀），所以对任何历史版本的命名都成立。

**真机状态（2026-11-04）**：macOS 上真跑了一次 `python3 install.py`（升级路径，非模拟）：旧命名的
plist 被 `_remove_legacy_services()` 删除并写进回执的 `legacy_services_removed`，新 plist
`io.feishu-agent.bridge.plist` 由 `plutil -lint` 校验通过，回执 `label` 已更新。**服务没有启动**
（`feishu-bridge start` 才会加载；本机当时处于停用状态，不为了让判据好看而自动开），所以
"新名字能被 launchd 拉起来"这一条靠首次 `feishu-bridge start` 或 Windows 下次升级时验证。
Windows 那台改名时离线，下次升级由 `finalize-systemd.sh` 调 `migrate-legacy-units.sh` 自动迁移，
升完跑一次 `verify.sh` 应看到 `legacy_units=0`。单元测试：`tests/test_legacy_units_migration.py`。

### 9.2 离线包的「真安装分支」尚未实测（2026-11-04 开账，触发条件明确）

`pack-offline.sh` 打出的包里有两个 MSI（WSL 3.0.3.0、OpenSSH 10.4.0.0），安装器会在「目标机确实没有
可用 `wsl.exe`」和「要装 Windows OpenSSH Server」时分别 `msiexec /i` 它们。这两条分支**在参照机上无法实测**：
参照机的 WSL 与 sshd 都是生产状态（有同事的 bridge 在跑），按契约不许改。

- 已做的替代判据：`Get-FileHash` 与上游 digest 一致、`Get-AuthenticodeSignature=Valid`、Windows Installer
  数据库可读（ProductName/Version/ProductCode 一致）、`SHA256SUMS`/`MANIFEST` 全量校验通过、
  `.wsl` 用 `wsl --import` 真导入成功（见 §8.1 判据）。
- **触发即测**：哪台目标机的 `00` 输出里出现 `WSL 未安装` 或 `01` 走到 `.msi` 分支，装完把
  `wsl --version` 与 `Get-Service sshd` 的结果发回，补进本节。目标机若 `wsl --status` 正常、sshd 已在跑，
  这两条分支永远不触发——这也是"跳过分支"本身被 E 阶段断言覆盖的原因。

### 9.3 同事机台账还差一行（等名字）

`docs/windows-hosts.csv`（本机文件、不进 git）要为这台新机器补一行：人 → 机名 / MagicDNS / 飞书 open_id /
通道命令。现在缺的只是"这台机器是谁的"这一个输入；包本身与人名无关（用户名默认取 Windows 账号名，可回车确认）。
`python3 tools/windows_host.py <名字>` 是查这一行的唯一入口。

## 10. 2026-11-05：教材通道（teach）与它顺手挖出来的一个进程级 abort

**要做的事**：让每台桥接自己「长」起来——账号本人在私聊里教它做事的方式、纠正它的错，
教进去的东西在之后每一条消息上都生效，而不是每次重来。设计稿
[superpowers/specs/2026-11-05-teach-channel-design.md](superpowers/specs/2026-11-05-teach-channel-design.md)、
实施计划 [superpowers/plans/2026-11-05-teach-channel-plan.md](superpowers/plans/2026-11-05-teach-channel-plan.md)。
产品事实（文件形状、入口判定、CLI、payload 字段）以 [CONTRACTS.md](../CONTRACTS.md) §「教材通道」为准，
本节只记**为什么这么定**和**踩到的坑**。

六个提交，按顺序可单独回滚：

| 提交 | 内容 |
|---|---|
| `61f3830` | 控制通道分页收全（`chat_messages` 返回 `(messages, complete)`，窗口没收全**不许**推进游标） |
| `eebc84b` | `bridge/teach.py`：只追加的教材日志 + 确定性 kind 推断 + 回执/清单/忘掉 |
| `265a853` | `feishu-bridge teach enable/list/show/disable/status`（enable 必须是 p2p 私聊，否则拒绝） |
| `a591306` | 教材入口判定（@ 自己 + 白名单会话 + 本人发送，缺一条都不算） |
| `03079d0` | 事件视图抽成 `bridge/events_view.py`（gateway 与 CLI 共用一份）、claim 注入 `teachings` |
| `f094f27` | 六个 prompt 渲染点接教材，规则文本单一来源 `TEACHING_RULES`；`ask_owner` 扩到五类 |
| `a05dd53` | 问答回路：本人引用回复求助 = 落教材 + 回执 + 重放原事件 |
| `e2f0ece` | 机械归并 `teach tidy`（不改写一个字）+ 下面那个 abort 的修 |
| `88308ed` | 重放的事件带 `owner_answer`，提示词要求「按答复办、别再问一遍」 |

**三个决定值得记住**

1. **教材只能用「自己 @ 自己私聊」教**，而且 bridge 自己发的消息**永不 @ 人**。这两条凑一起，
   自激循环就不是"靠标记位去防"，而是结构上不可能发生（不用 marker、不用回显账）。
2. **kind 是确定性推断的，不调模型**：前缀（`原则/风格/反馈/事实/资料/指针`）决定 kind 与档位，
   回执每次都把判定结果说回去，教错了当场纠正。这一条挡住"模型自己猜你什么意思"。
3. **归纳拆成两层**：机械归并（删重复、合并同一天同类、一个字不改写、原条目保留）当天做；
   模型归纳**不做**——它必须带来源 id、必须在私聊里看到 diff 并回「确认」，否则错的规则会
   以本人身份说出去，而「它什么时候偷偷改了自己的准则」没法审计。

### 10.1 坑：一条 5000 层嵌套的「回复」能把整个测试进程 abort 掉

**现象**：`python3 -m pytest -q` 偶发 `Abort trap: 6`（exit 134），失败点固定在
`tests/test_consumer_safety.py`，单跑那个文件却全绿；`-v` 也绿。日志里没有任何 traceback。

**定位**：macOS 崩溃报告（`~/Library/Logs/DiagnosticReports/Python-*.ips`）的故障线程是
`_json` 扫描器互相递归 → `_Py_CheckRecursiveCall` → `Py_EnterRecursiveCall` → `_Py_FatalErrorFunc`
→ `abort()`，即 `Fatal Python error: Cannot recover from stack overflow`。触发输入是那条测试用例
自己喂的 `'{"a":' * 5000 + …`：`json.loads` 会把 C 栈递归到底，Python 3.9 在这种状态下**抛不出
RecursionError，只能 abort**。能不能活取决于当时还剩多少栈——所以 `-q`/`-v` 结果不同、
整包跑必崩而单跑不崩。

**修法（根治）**：`consumer_safety._exceeds_nesting()` 在**交给 `json.loads` 之前**用迭代扫描
判嵌套深度（上限 64 层），超了直接 fail-closed（`ValueError`）。一句"回复"不可能有 64 层对象，
所以这不是放宽也不是收紧，是不让病态输入碰到解析器的递归。判据：
`tests/test_consumer_safety.py::AnswerContractTests::test_pathological_nesting_is_rejected_before_the_json_parser`
+ 连跑三次全量（`878/878/878 passed`）。

### 10.2 ④ 真机闭环：已在真机跑通（2026-11-05，macOS，本机生产环境）

**发布**：`python3 install.py` → release `b386d153…`（首次）→ 运行中发现 `teach enable` 报
`name 'threading' is not defined`（`_teach_transport` 只在真机上跑，单测都把它替换掉了）→ 修好
重新发布；后续两次修复（@ 占位符、run 产物记账）各重新发布一次，当前 `d5583250e65f8257`。
每次 `doctor --assert` 都是 **exit 0 / verdict ok**（`events_dead=21` 是历史死信，`agent_binary`
在 external 模式下不需要本机可执行文件）。回滚点始终保留上一代 release。

**教学会话**：`config.json` 里两个 `self_chat_ids` 都是**群**（`chat_mode=group`），不能用。
用 `im/v1/messages/search`（`chat_type=p2p`）列出近 7 天 p2p 会话，挑出「所有发送者都是本人」的
两个候选，再按内容分布选中聊天为主的那个（另一个是传文件用的）：

```
feishu-bridge teach enable --chat oc_****（p2p 私聊，chat_mode 由 chat_info 校验）
已启用教学：oc_****
· 在这个私聊里 @ 自己说一句就是教它；说「教材清单」看全部，「忘掉最后一条」撤回
· 教材只影响做法与偏好，不改变权限（删除/密钥/MCP 仍然禁止）
```

> **上面这段输出是 11-05 的样子，别照抄**：「忘掉最后一条」这个口令随协议信号一起在 §25.7 删了，
> 名字取代编号之后（§25.9）撤回的说法是「撤回 汇报先给结论」——条目名由系统在回执里给出。

**本人发出第一条教材后**（私聊里 @ 自己 说「原则：…」），30 秒内收到回执、`teach status` 落地：

```
$ feishu-bridge teach status
{"enabled": true, "chat_id": "oc_****", "corrupt": false, "rev": 1,
 "charter_count": 1, "notes_count": 0, "bad_lines": 0, "pending_questions": 0, "error": null}

$ feishu-bridge teach list
教材共 1 条（版本 1）
· t-0001 · 原则 · 可以直接说：你可以用本机的 Documents 目录下的文件作为你的信息库，…
```

**claim 注入的实据**（拿真库里的教材块过 `events_view.event_view`，与 `events claim` 同一条路径）：

```json
{"rev": 1, "charter": [{"id": "t-0001", "kind": "principle", "expose": "say",
  "text": "你可以用本机的 Documents 目录下的文件作为你的信息库，…", "at_ms": …, "ref": {"chat_id": "oc_****"}}],
 "notes": [], "error": null}
```

`agent_prompt.teaching_section` 渲染出来的就是模型真正看到的那段：六条规则 + 「教材·版本 1」+
「本人的偏好与准则」+ 那条正文。`teach tidy --dry-run` 在只有 1 条时 `before=1 after=1`、什么都不动。

**真机抓到的两个坑（都已修 + 已重新发布）**

1. `@` 占位符进了教材正文：消息内容里的人是 `<at ...>`，取文本后变成 `@_user_1`，于是教材正文成了
   「@_user_1 你可以用本机…」，并且每次提示词都会带上它。修法：`teach.clean_text()` 一处定义，
   **写的时候清一次、读的时候再清一次**（旧条目不用重教也干净，实测 `teach list` 立刻变干净）；
   `教材清单`/`忘掉 t-0007` 这类精确指令也先清再匹配（否则 @ 一下就匹配不上了）。
2. `teach enable` 在真机上才炸（`threading` 漏 import）：单测把 `_teach_transport` 替换成了假
   transport，于是"测过的"和"真跑的"不是同一条路。补了 `TeachTransportFactoryTests` 直接调真函数。

**真机第三次踩坑（产品行为，不是 bug）**：想验证它答得怎么样，本人直接在**教学私聊**里问了
「你的信息库在哪？你能读什么、能不能改东西？」。教学通道把这句话当教材收成了 `t-0002`
（`teach rev=2`），同时因为该 p2p 不在 `self_chat_ids` 里、采集只搜 `chat_type=group && is_at_me`，
所以它**不会回答**——教学私聊是"教"的通道，不是"问"的通道。`feishu-bridge teach revert t-0002`
撤回后 `rev=3/charter_count=1`。这条已写进 CONTRACTS「教材通道」：要验证回答，去白名单群里 @ 自己。

**最后一条判据：已补齐（2026-11-05 18:20 起）**——真实消息的回复确实按教材办了。
`t-0003` 教的是「以后不要称呼我为刘总，称呼我为宝哥！」，之后每一次真实回复的开头都变了：

```
$ sqlite3 state.sqlite3 "select substr(body,1,40) from outbox where state='reply_verified' order by rowid desc limit 3"
om_…c64f | /tmp/task-demo/report.txt
om_…6d5  | 宝哥，第 2 步做完了：我已经把四行汇总表追加到 `/tmp/task-demo/summary.md` 末尾…
```

教材本来在 prompt 里，回复里就出现「宝哥」；这不是巧合也不是签名，是教材在起作用。L2-a（记忆检索）
上线后这条依然成立——准则每轮必给，只是资料改成按消息检索了。

---

## 11. 教学私聊的"判读 + 确认"（2026-11-05）

**触发**：真机第一次用就踩到——本人在教学私聊里问了句「你的信息库在哪？你能读什么、能不能改
东西？」，被原样收成教材 `t-0002`。本人随即指出："不是每一句都是教材吧，也可以是指令、是问题、
是责怪它不够聪明。" 这个判断是对的：当时的通道只有一种语义。

**决定**：教材以本人身份长期生效，误记的代价不对称（漏记只是再说一次），所以**默认不记**：

| 说的话 | 处理 |
|---|---|
| `原则：/风格：/反馈：/事实：/资料：/指针：` 前缀 | 直接落教材（正式教法，零摩擦） |
| `教材清单`/`忘掉最后一条`/`忘掉 t-0007` | 精确命令，照旧（**这三种口令在 §25.7 已删除**；现在的说法见 §25.9） |
| `记` / `不记` | 确认/丢弃上一条候选（单槽，被顶掉时回执明说） |
| 其他（提问、派活、吐槽、闲聊） | **不落盘**，确定性判读成 提问/派活/评价/陈述，回执说清并给出口 |

判读**不调模型**：关键词 + 句式，`classify()` 二十次调用必须给同一个答案（有测试钉住）。
评价词优先于疑问词——「你怎么这么笨？」既有「怎么」又是在骂人，按评价处理才接近本意。

**没做的（明确留给下一步）**：教学私聊仍然**不回答问题、不接派活**。要它答话/干活，仍然去白名单
群里 @ 自己。原因不是能力不足，是入口问题：采集端只搜「群 + @我」，私聊是教材通道单独轮询的，
要让私聊派活就得把消息登记成真正的 `message`（inbox 行 → 事件 → 消费者 → 回复），并把
"哪些算教材、哪些算任务"的分流做在同一条消息上。这是一份独立的小设计，不在本次范围。

**验证**：`python3 -m pytest -q` → 914 passed；`tests/test_repo_hygiene.py` 3 passed；
`TeachConfirmationTests` 8 条覆盖（陈述→候选、记→落盘、不记→丢弃、提问/评价措辞、
顶掉提示、空候选、重复记、前缀免确认）。

---

## 12. agentkit 的 dsh 适配器把 `thinking` 当成非法 status（2026-11-05，真机第一次 @）

**触发**：同事 macOS 机器（DSH 消费者）装好后的**第一条真实群 @ 消息**，bridge 侧只看到
`AgentFailed`，事件回到 pending 重试到死信；但同一轮的 `run.json` 里 `final` 明明是合法
`{"answer": "…"}`，连 `turn_end reason.kind=completed` 都在。

**判据**（不用猜）：把失败那轮的 NDJSON 原样回放给适配器，看 `state.extra["fatal"]`：

```
EVENT init      fatal: None
EVENT other     fatal: None                 # status turn_start
EVENT other     fatal: None                 # status step_start
EVENT other     fatal: invalid dsh status   # ← thinking
EVENT text      fatal: invalid dsh status
```

**根因**：`legacy.py` 的 `parse_line` 把 `status` / `thinking` / `tool_result` 一律映射成
`kind="other"`，而 `on_event` 的最后一个分支默认「不是 init/text/tool/result/error 就是 status」，
`thinking` 没有 `phase` → 命中 `invalid dsh status` → 整轮 FAILED。legacy 入口
（`dsh-consumer.py`）对这两类事件的语义一直是「只落盘、不做动作」，两个入口对同一条流给出
两种结局，这本身就是缺陷。

**修法**（一次修到语义，不留兼容分支）：`status` 单独成 `kind="status"`；`thinking` /
`tool_result` 成 `kind="note"`（放行）；未知类型仍然 fail closed（`kind="error"`）；
`on_event` 里非 status 的杂项显式报 `unexpected dsh event`，不再误落 status 分支。

**验证**：`tests/test_agentkit_legacy_e2e.py` 新增 `test_dsh_thinking_and_tool_result_are_benign`
（thinking + tool_call + tool_result + text 混在正常流里仍 COMPLETED，18 passed）；同一条真机
失败流回放：改前 `failed/agent_error`，改后 `completed/ok`，answer 原文一致。

**真机复测**（同一天，releases `a81fa57b9982a20a`）：群里 @ 一次 → `events list` 该事件
`done`、`claims/acks=1`，`inbox` 行 `completed` + `outbox` 行 **`reply_verified`**，回复正文
带署名「此回复信息来自账号本人的 AI 助手」。引擎不撒谎的地方就在这里：`reply_verified` 是
bridge 自己发出去又核对过的，不是消费者自述。

## 13. 私聊从"只收教材"改成"教材 + 派活 + 提问"的唯一入口（2026-11-05）

**起因**：上一节把「默认不记」做出来之后，本人立刻问了下一步——"私聊里除了教材，也该能
直接派活、直接提问吧？" 这不是加一个前缀的问题，因为**当时的私聊是第二个入口**：
`control_tick` 每 30 秒自己轮询一次 `self_chat_ids + teach chat`，用它自己的
`_control_seen` 做去重、自己的准入、自己的回执；而群消息走的是
`transport.search → inbox → details → events`。同一件事两条链路 = 下一次改动只会改一条
（本仓 AGENTS.md §0.5 已经写明这是"未完成的重构"）。

**改法**（先设计、后动手，设计稿 `docs/superpowers/specs/2026-11-05-self-chat-task-channel-design.md`）：

1. **`transport.search` 支持 `chat_id`**：`filter` 从 `{chat_type:'group'}` 变成
   `{chat_ids:[oc_…]}`。真机实测过：`{"chat_ids":[oc_…],"is_at_me":true}` 能精确取到那一个
   会话里 @ 本人的消息，不带 `chat_type` 也成立。
2. **采集器新增 `self` 扫描**：只在教学启用期间存在（启用 `begin_scan`、停用 `reset_scan`），
   30 秒一拍、**不回填**（起点 `max(service_epoch, enabled_at_ms)`）。它和 `live`/`backfill`
   共用 `scan_bounds`、共用失败退避、共用"挂起即冻结"（挂起时三个 next 全归零）。
3. **`details` 成为唯一判定点**：`self_chat` 的判定加上教材私聊（原来只看 `self_chat_ids`，
   于是私聊消息会被当成"别人的消息"）；在生成事件之前调 `route_self_chat`，返回 `None`
   表示就地办掉（`complete_details(state='ignored', error='teach_channel')`），返回 dict
   表示交给事件管道（可覆盖正文与 `self_intent`）。
4. **删掉旧的教材轮询**：`control_tick` 只剩 `下班/上班`（控制通道），
   `handle_teach_answer`/`handle_teach_message` 合并成 `route_self_chat` + `try_answer_question`
   + `_run_self_command`，`is_control_message` 的会话判定改为 `control_chat_ids()`
   （自聊群 + 教材私聊），于是私聊里说「下班」也照常生效。

**分流顺序**（命中即返回，写进 CONTRACTS）：挂起指令 → 已处理回放 → 非文字 → 引用答复 →
精确命令 → `问：`/`做：` → 带前缀的教材 → 默认不记（候选 + 回执）。

**三个关键取舍，都是"判错的代价不对称"**：

| 取舍 | 选了什么 | 为什么不选另一头 |
|---|---|---|
| 还要不要「@ 自己」 | **保留** | 它是零账本的自激防线（bridge 的消息永不 @ 人）。去掉就要维护"哪些是我发的"台账，而台账漏一行就是自激 |
| 派活/提问怎么识别 | **显式前缀** `问：`/`做：`，或候选上回「答」/「做」 | 猜句式会猜错；猜错的代价是"以为在聊天，实际替本人派了活" |
| 回放窗口怎么处理 | `teach.already_handled()`：教材/答复/候选里有同一个 `message_id` 就静默跳过 | 不判的话，升级那一刻会把旧通道回过执的消息再回一遍 |

另外补了两条小账：候选 `teach_pending.json` 带 **30 分钟过期**（隔夜回一句「记」记下的不是
当时的语境）；`status.json` 加 `teach_channel`、`doctor` 加 `teach_scan` 一项
（"教学开着但扫描没注册"以前只能翻日志）。

**测试全部换了驱动方式**：`TeachIntakeTests / TeachConfirmationTests / TeachAnswerTests /
TeachIntentTests / SelfScanTests` 现在都经 `details` 跑（`TeachEntryCase.deliver`），
不再用 `control_tick` + 假轮询——否则"测过的"和"真跑的"又是两条路。全量 `927 passed`。

**真机的第一课**：第一版让 `sync_self_scan` 在"启用"时就用 `begin_scan('self', now, now)` 注册，
而 `collect_scan` 的首次分支也自己算一遍窗口——两处算同一个窗口，结果注册出来的
`start_ms == end_ms`，飞书每 10 秒拒一次（`TransportError: start_time must be earlier than
end_time`），日志里只有 WARNING，`scan` 行看着还在，`status` 里 `teach_channel` 也照样
`enabled: true`。**注册只留一处**（`collect_scan` 的首次分支，那里有 `scan_bounds` 和
origin 夹取），`sync_self_scan` 只负责"停用就清游标"；顺带把 `scan_active` 从健康判据里删掉
（一个窗口搜完 `active` 就是 0，那是正常节奏），改成报 `watermark_lag_s < 300`。
测试钉住：`SelfScanTests` 断言 `start < end`。

第二个坑是同一个 bug 的**遗留状态**：重装新版本后，`scans` 里那条 `start == end` 的旧行还 active，
`collect_scan` 看到 `active` 就直接拿它去搜，于是新代码继续每 10 秒失败一次（要么等
`MAX_SCAN_FAILURES=5` 次退避后自愈，两三分钟）。这条不能靠"人手动 reset"解决：加了
`_usable_bounds(scan)`——游标 `start >= end` 或缺失就当作不可用，当场重算（`scan_bounds` 是
唯一窗口算法）。判据：`SelfScanTests::test_a_poisoned_cursor_is_recomputed_instead_of_failing_forever`。

**真机第二课（本人连发四句之后定的口径）**：本人没有按我给的「问：」「做：」前缀写，
而是直接说人话——「把今天的 inbox 数量报给我。」「你能读什么？能不能改东西？」
「以后不要称呼我为刘总，称呼我为宝哥！」「你笨笨的」。四条都进了私聊管道、都回了执，但暴露两个问题：

1. **回执文案还是旧世界的**：它告诉本人「私聊现在只用来教，不回答」「要真派活：去群里 @ 自己说」
   ——那正是这次改动要消灭的限制。功能改了、说明书没跟着改，用户看到的就是假的。
   （修法：回执直接给一键出口；测试里那条断言「回执包含『群里』」原本钉住的正是过期文案，
   改成断言新文案 + 反查「去白名单群」不再出现。）
2. **"默认不记"一刀切切错了地方**：一句明明白白的提问也要「回『答』」才答、一句明明白白的
   「以后…」也要「回『记』」才记——多一次往返，通道读起来就不像个人。

于是三条口径（本人拍板，2026-11-05）：

| 判读 | 现在的行为 | 为什么 |
|---|---|---|
| 提问 | **直接答** | 答错最坏是多说一句废话；多一次往返换不来安全 |
| 祈使式长期要求（以后/今后/每次/记住/别再…，问号结尾除外） | **直接记** | 这就是教材句式，几乎不可能是随口陈述；回执说清记了什么、可用「忘掉」撤 |
| 派活 | 仍先确认，回「做」才动手 | 会真的对外发消息/动文件，误判代价比答题高一档 |

第三课是**词表和口气**。本人 15:59 又发一句「你蠢蠢的，你不像是 deepseek 的模型，你一点都不智能」，
被判成 `statement`（抱怨词表里只有「笨/慢/又错…」，没有「蠢/垃圾/没用/不智能…」），于是回执给出的是
「回『记』按原则记下」——等于把抱怨原文当规则收。同一句话也点出另一个问题：回执是 3-4 行机制说明，
读起来像系统通知，不像在跟自己说话。修法：抱怨词表补齐（蠢/傻/呆/垃圾/废物/没用/不智能/不靠谱/
答非所问/敷衍…），回执压到 1-2 行、先给动作（回「做」/回「记」/回「不记」），抱怨那条改成
「哪儿不对请直说（哪一步/哪一句），我照那句改」。判据：
`VerdictTests::test_mild_insults_are_complaints_not_recordable_statements`。

判据：`tests/test_teach.py::VerdictTests` 三条新断言 + `tests/test_service.py`
`test_a_natural_question_is_answered_without_a_prefix`、`test_an_imperative_long_term_request_is_recorded_directly`。

**真机第三课（最严重的一条）**：私聊里问的那句「你能读什么？能不能改东西？」，
事件、消费者、回答全都正常生成了，但 **sender 把它判死了**——
`needs_attention / Group no longer in confirmed inventory`。原因是 sender 的防呆：
"这条消息所在的会话必须在已确认在册的群清单里，否则可能是群没了、别乱发"。
教材私聊是 p2p，**永远不在 inventory 的群清单里**，于是每一条私聊回复都被当成"群已不在清单"。
单元测试没抓到，因为假 transport 从没走过 p2p 会话；这也是"测过的 ≠ 真跑的"的又一次现场。
修法：`Service.replyable_chats()` = 在册群 ∪ 教材私聊，sender 用它判；测试用 p2p 会话 +
回读校验（`reply.chat_id == original.chat_id`）钉住。

**真机第四课（最隐蔽的一条）**：修好 sender 的群清单判定、把那条答案重新排队之后，日志说
`reply sent`，但 outbox 又报 `Reply readback mismatch`，而且飞书里根本没多出这条消息。
原因是**幂等键只按消息 id 生成**：`reply_uuid(mid)` 既被判读回执用（`_teach_reply`），又被答案用
（outbox），而那条消息 15:50 已经收过一份判读回执 —— 同一个 uuid。飞书按 uuid 幂等，第二次
投递被当成重复，直接返回 15:50 那条老消息；bridge 回读发现正文对不上，判 mismatch 并不重发。
**同一条消息本来就可能有多条不同用途的回复**（回执 / 答案 / 挂起通知），幂等键必须带用途：
`reply_uuid(message_id, purpose)`，回执用 `'teach'`、挂起通知用 `'control'`、答案保持 `'answer'`。
判据：`tests/test_service.py::ReplyUuidPurposeTests`。

**已知没做**（明确留给下一步）：真机跑一遍 §3.7 的七条判据（尤其"群里问、私聊答"和
"私聊派活 → 消费者真的去做"）；Windows 实机；`self_intent` 在 DSH/Codex 两侧的 prompt 效果
只能靠真机日志确认。

---

## 13. 署名第二次踩同一个坑：正文跟了账号，署名还在说「账号本人」（2026-11-05）

**触发**：§12 修完、真机第一次 @ 验收通过后，本人看回复正文没问题，却指出末尾署名是
「此回复信息来自账号本人的 AI 助手」——「你没有应用人家的飞书账号」。**署名是兜底值**，
不是取不到账号：同一台机器的 `status.json.identity.userName` 明明就是正确的人名。

**判据**（一条命令就能看出分岔）：`feishu-bridge doctor --assert` 的 `reply_identity` 当时输出

```
自称「<本人>的 AI 助手」（来源：本机飞书账号身份）/ 署名「内置兜底」
```

——自称那一半背后有账号身份，署名那一半背后只有 `config.json` 的静态字符串。`config.json` 里
`signature` 缺失（`signature 字段存在: False`），于是永远走内置常量。

**根因**：§4 修的是**正文自称**那条路（身份从 `status.json` 运行时取、删掉 `owner_name`
配置面），**署名那条路没跟着改**：它还是「安装者写一段文案进 config」。于是同一台机器上
"是谁的 AI 助手"有两个答案——正文按账号，署名按配置。`config.json` 里没人名时署名里
就没有人名，这不是 bug 而是当年那版设计没覆盖的地方（Windows 机看起来对，是因为装的时候
显式给了 `--signature "来自<机主>的AI助手"`，是配置值的功劳）。

**修法（根治，不留第二份事实）**：

1. 署名变成**模板**：内置值 `此回复信息来自{owner}的 AI 助手`，`{owner}` 占位符在
   `bridge/agent.py:resolve_signature` 渲染时替换成账号姓名；取不到身份 → 中性词
   `账号本人`（渲染结果与升级前逐字相同，所以"取不到"时行为不变）。
2. 账号姓名收成**一份实现**：`agent_prompt.owner_name_from_status(home)`；
   `consumer_runtime.owner_name_at`（正文自称）与 `bridge/agent.py:owner_name_at`
   （署名，4 个 `render_reply` 调用点：内置 analyst、HTTP 网关、`notify`、`events reply`）
   都只转发到它。`tests/test_agent_prompt.py` 静态钉住"除 `agent_prompt` 外不许再有人读
   `status.json` 的 identity"。
3. `doctor` 的 `reply_identity` 改成打印**渲染后真要发出去的那一行**（附带"配置|内置模板"），
   安装器的未指定署名 warning 也说清"模板会带出账号名"。
4. 显式配的固定署名（例如 Windows 机上的「来自某某的AI助手」）**字面值一字不改**——
   升级不重写既有署名的行为。

**验证**：`python3 -m pytest -q` → 940 passed；`tests/test_repo_hygiene.py` → 3 passed。
判据是"两端各自能复跑"的：

- `tests/test_gateway.py`：网关（独立进程，没有进程内 identity）回复 → `outbox.body` 末尾
  = 「此回复信息来自<身份姓名>的 AI 助手」；无 `status.json` 身份 → 中性兜底。
- `tests/test_agent.py`：`resolve_signature` 四种情况（有身份 / 无身份 / 旧固定署名原样 /
  非字符串身份按"取不到"处理）。
- `tests/test_cli.py`：`doctor` 在"没配署名"时打印「此回复信息来自<身份姓名>的 AI 助手」（内置模板）。

**真机复测（macOS 同事机，2026-11-05）**：新 release 装完（`running_release == installed_release`），
`doctor --assert` 的 `reply_identity` 变成

```
自称「<本人>的 AI 助手」（来源：本机飞书账号身份）/ 署名「此回复信息来自<本人>的 AI 助手」（内置模板）
```

然后在测试群真发一条 @ 消息（`om_x100b61f73890f4acdd62b5984105052`），她的 bridge 回了
`om_x100b61f73680e8b0dd6f1f6aea8d3d4`，发送身份 `sender_type=user`（= 本人账号）、`reply_to` 正确，
正文结尾正是「此回复信息来自<本人>的 AI 助手」。同一台机器的 outbox 两行并排就是前后对照：

```
旧 release: …此回复信息来自账号本人的 AI 助手      (reply_verified)
新 release: …此回复信息来自<本人>的 AI 助手        (reply_verified)
```

**教训（写给下一次装别人机器的人）**：身份数据取到了，不等于**每一处引用身份的地方**都取到了。
同一个问题（"这台机器的 AI 自称谁"）在一份回复里出现了两次（正文 + 署名），就必然有两个
实现点；改一处而没把另一处指向同一份实现，就是给自己留了第二次事故。判据要**逐处**打印
最终文本（`doctor` 打印渲染结果，而不是模板），否则"配置看着对、发出去不对"永远查不到。

## 14. bridge 2.0 第一步：删掉内置大脑，桥只留通道（2026-11-05）

**问题**：桥里有两个"会回答"的东西——`bridge/agent.py`（内置 Cursor agent，`Service.analyst()`
线程）和外部消费者（`dsh-consumer.py` 这类）。它们能力不同、prompt 不同、工具策略两份，
谁答一条消息取决于 `home/external.json` 里的一个开关；出问题时第一件事是"这到底是哪条路答的"。
离"桥是通道、大脑是 Agent"的目标越走越远。

**做完的事**（一次性删除，不留开关、不留兼容层）：

| 删掉的 | 理由 |
|---|---|
| `bridge/agent.py`（Cursor agent、工具策略、stream-json 解析、超时/预算/finalize 第二遍） | 桥内不再有生成器；生成能力属于消费者 |
| `Service.analyst()` 线程、`self.agent` | 同上；`status.workers` 从 4 个变 3 个 |
| store 的 analysis 状态机（`analysis_queued/running`、`claim_message('analysis')`、`complete_analysis`、`abandon_message`、`rollback_external`） | 多出来的那一层状态只为内置 analyst 存在 |
| `feishu-bridge events enable/disable` | "开不开外部接管"这个开关没有意义了：外部消费者是唯一大脑 |
| `install.py` 的 `--cursor-binary` / `CURSOR_BINARY` 与 cursor-agent 硬要求 | 只有内置 agent 需要它 |
| `tests/test_agent.py`（727 行） | 测的是被删的那份实现；署名/脱敏/长度上限的用例搬进 `tests/test_reply.py` |

**留下的判据**（这一节就是"怎么证明它真的只剩一个大脑"）：

1. `feishu-bridge status` 的 `workers` 只有 `collector/details/sender`；`grep -rn "analyst" bridge/`
   只应命中注释里的"已删除"说明。
2. `doctor` 新增硬故障 `consumer_offline`：`message` 事件积压超过 120 秒没人领取 → `--assert` 退出 1。
   失败语义是有意的：消费者不在线时消息停在 `external_queued`，桥**不会**换一条路替你答。
   判据可复跑：`tests/test_cli.py::DoctorAssertTests::test_consumer_offline_is_a_hard_failure`。
3. 状态名 `analysis_completed` → `reply_queued`（`PRAGMA user_version` 3→4，迁移里 `UPDATE`）。
   改成它真正的含义（回复已写进 outbox、等 sender 发出），老库升级后 `grep` 不到 analysis 状态：
   `tests/test_store.py::MigrationTests::test_v4_renames_the_legacy_analysis_completed_state`。
4. 唯一写回复的入口是 `Store.complete_external_reply`（`BEGIN IMMEDIATE` 里做 app 预算检查），
   outbox 的幂等/不可变/3300 秒不确定窗口等不变量原样保留（`tests/test_store.py`，用例改成走
   外部消费者发布路径的 `publish()` 辅助函数）。

**真机（macOS，2026-11-05 17:15）**：`python3 install.py` 装出 `ae53de5be55a2f3a`，
`status.workers={collector,details,sender}`、`external_mode=true`、`PRAGMA user_version=4`、
`inbox` 里 `analysis*` 状态 0 行；`doctor --assert` verdict=ok，其中
`consumer_offline: 外部消费者在线（无积压）`、`workers_complete: 三个 worker 都在`。
`install.py` 顺手把 `external.json` 收敛成 `{"reply_timeout_seconds": 1200}`（老键 `enabled` 抹掉，
否则读文件的人会以为还能关），并把 `config.json` 里已无读者的 `agent_timeout`/`cursor_binary` 删掉。

**还没删的（带到期日）**：`codex-consumer.py` 现在只是"另一种外部消费者"，不再是桥的内置大脑，
但已经没有人用它，而且 `agentkit/adapters/legacy.py` 的 codex 适配器还在**按文件名加载它**拿
`_command`/`SCHEMA`/`MAX_OUTPUT`。删它要先把这些搬进适配器。到期日 **2026-11-30**：届时要么
一起删掉（codex-consumer.py + 适配器的加载依赖 + tests/test_codex_consumer.py + release.CONSUMER_FILES 里的名字），
要么在 AGENTKIT.md 里写明"为什么这个入口必须留着"。

**顺手挖出来的两个真 bug**（都是"加断言时才发现的"）：

1. **启动器把退出码丢了**。`bin/feishu-bridge` 生成的是 `main(str(home))`，返回值被丢掉
   ⇒ 一切靠退出码的自动化（`doctor --assert`、`install.py --verify`、`bridge-remote.sh check`、
   WSL2 的 `verify.sh`）**恒为 0**。症状是"装了新版本、断言全过"，但断言其实从没失败过。
   修成 `raise SystemExit(main(str(home)))`，并加一条**真跑启动器**的用例
   （`tests/test_install.py::test_launcher_makes_the_cli_return_value_the_process_exit_code`：
   桩 CLI 返回 3 → 进程退出码必须 3）。教训：只有直接调 `cli.main()` 的测试，永远测不出
   "入口把返回值丢了"。
2. **WSL2 的验收入口还在等 `analyst` 线程**（`dist/wsl2/wsl/verify.sh:68`、`dist/wsl2/setup.sh:464`）：
   全新 WSL 安装的第 8 步会 fail-fast、`verify.sh` 永远不通过。改成三个 worker，
   `setup.sh` 的"四个 worker 全 true"文案同步。**删一个线程要 grep 所有断言它的地方**，
   Python 测试会红，shell/PS 断言不会。

3. **消费者把已删除的 `external.enabled` 当门禁**（我自己引入的，真机端到端才暴露）。
   `_external_config` 收成"只剩 `reply_timeout_seconds`"之后，`consumer_runtime.remaining_seconds()`
   里那行 `if external.get('enabled') is not True: return 0` 让**每一条**消息都算出 0 秒预算，
   消费者立刻以 `InsufficientTime` 放弃 → `inbox=needs_attention` / 事件 dead，飞书里就是"没人回"。
   端到端实测：17:40:41 领，17:40:41 就放弃，17:42:57 死信。修法是删掉这个读取点（唯一事实是超时），
   判据 `tests/test_consumer_example.py::test_status_without_the_deleted_external_switch_still_uses_the_full_budget`。
   **教训**：删一个配置键，要把"读这个键的所有地方"一起 grep 掉——`bridge/` 里删干净了，
   但消费者在 release 里是**另一个文件**（`consumer_runtime.py`），单测又恰好 stub 了旧形状的
   `status` 输出，于是两层都绿、线上全红。只有真发一条消息才看得见。

**真机判据（2026-11-05 17:5x，release `58a730b24603fb68`）**：往活库插一条 300 秒前创建、
租约到明天的 `message` 事件（消费者不会领它），`feishu-bridge doctor --assert` →
`EXIT 1 / verdict fail / consumer_offline fail / lag 300.1s`；删掉该行后同一命令 `EXIT 0`。
这一条同时证明了启动器退出码修好了、`consumer_offline` 在真库上真的会拦。

**端到端实测（2026-11-05 17:46，release `ca78dd3dfd8f2301`）**：用本人身份在"自己的会话"里发一条
@ 自己的消息（`om_x100b61f1493d04a8c0f4618cad11922`，正文「bridge 2.0 自检第二轮：3+4 等于多少？只回一行。」），
45 秒走完全链路：

```
17:47:03 inbox=details_running
17:47:06 inbox=external_queued   event=pending
17:47:09 inbox=external_queued   event=leased  consumer=dsh-main
17:47:24 inbox=reply_queued      outbox=send_started  event=done
17:47:27 inbox=completed         outbox=reply_verified reply_id=om_x100b61f14409b8bcdfbef3e742d6ce8
```

回复正文（outbox 里那一行，署名是这台机器 config 里的模板）：

```
7
此回复信息来自<身份姓名>的 AI助手
```

也就是说：桥没有生成任何一个字（`grep -c "agent" bridge/` 里已经没有生成器），
`7` 完全来自 `dsh-consumer.py` 起的 DSH 会话。这是 L0 的验收点。

**教训**：一个"能自己回答"的默认实现，会把"通道"和"大脑"的边界永久糊住——它的存在本身
就是一个兜底分支，而兜底分支不会有人去删。"谁回答"必须是**进程外的一个消费者**，这样
"没人回答"才是可观察的状态（`consumer_offline`），而不是被另一条路悄悄接住。

## 15. bridge 2.0 第二步：任务账本（L1，2026-11-05）

### 15.1 问题

回合制下"一个活分三步"没有记忆：第一条消息里读的文件、第二条消息里做出的决定，第三条消息时全丢了。会话续跑能解决一部分，但飞书是回合制环境（D2）——续跑意味着把一个长会话挂在常驻进程上，token 贵、换大脑就断、还不可审计。

### 15.2 做法

账本，不是进程：`tasks` 表 + `task_messages` 映射，一轮消息 = 一个事件，任务只是"这个活做到第几步"的记录。四件事：

1. **归属只有一个判定点**（`Service.task_context`）：消息引用了任务里的消息 → 挂同一个任务；否则私聊里 `做：/干活：/安排：` → 开新账；都不满足 → 不建账。
2. **载荷带账本**（`payload.task` / `payload.tasks`）：消费者冷了也能看到目标、走了几步、产物、卡住的问题。
3. **消费者回填**（答复 JSON 的 `task` 对象 → `events reply --task-*`）：状态、这一步做了什么、产物路径、要问本人的那句话。
4. **失败与停滞有状态可查**：dead/超时 → `steps[].outcome=failed|timeout` + 任务 `failed`；`running` 超过回复窗口没动静 → 回 `open`（`attempts+1`），连续 3 轮 → `blocked` 并自动写好问题。

顺带解决一个真实副作用：**引用任务消息说话不再进教材候选**。以前在教材私聊里引用着活说"继续"，会被判成一堆新教材；现在它是对那个活说的话。

### 15.3 一个刻意的"不猜"

消费者没回填状态时，桥**不替它写 done**：只有单步任务（第一句派活就答完）才收口成 done，多步任务中途的一步状态原样不动，交给停滞回收。理由：谎报做完了比显示"还在做"贵得多——前者会让本人以为活做完了，后者只是让账本多挂一会儿。写进 CONTRACTS 的"默认语义"。

### 15.4 判据（可复跑）

```bash
python3 -m pytest tests/test_tasks.py -q                    # 15 条：建账/归属/一步一条账/幂等/失败/回收/计数/迁移
python3 -m pytest tests/test_service.py -q -k TaskLedger     # 4 条：派活建账、引用续做同一条账、坏账本不丢消息
python3 -m pytest tests/test_cli.py -q -k TaskCommand        # 8 条：task 命令与 --task-* 落账
python3 -m pytest tests/test_consumer_task.py -q             # 答复 JSON → argv，逐字段丢弃
python3 -m pytest -q                                         # 全量
```

真机（release 见 §15.5）：私聊里 `做：…` 一句 → `tasks` 多一行、`payload.task.step=1`；回复里带
`task.state=running` + 产物 → `feishu-bridge task show` 里能看到 step 与 artifact；引用那条任务消息再说
一句 → 同一个 `tsk_…` 上出现第 2 步，没有开出第二个任务；把一条 `running` 任务的 `updated_at` 拨到 15 分钟前 →
心跳把它改回 `open`，`attempts` +1。

### 15.5 真机实录（2026-11-05 18:14–18:25，release `25e0ffbf39e98e1f`）

一个真活跨 3 条消息，2 个产物（就是设计稿 §4.5 第 1 条）：

| 步骤 | 我发的（教材私聊里 @ 自己） | 账本 |
| --- | --- | --- |
| 1 | `做：`把 `/tmp/task-demo` 三张 csv 汇总成 `summary.md`，先只做文件 | `tsk_677e22c3a6ba` 建账，`payload.task.step=1`，回复报 `running` + 产物 `summary.md` |
| 2 | 引用第 1 条：「把四行汇总表追加到 summary.md 末尾并贴给我」 | 同一本账 `step=2`（没有第二个任务），回复贴出 Q1/Q2/Q3/累计 四行 |
| 3 | 引用第 1 条：「再补 `/tmp/task-demo/report.txt`，只回路径」 | 同一本账 `step=3`，产物 `report.txt`，任务收口 `done` |

`feishu-bridge task show tsk_677e22c3a6ba` 的终态：`state=done`、3 条 step（每条带 `message_id/outcome/note`）、
`artifacts=[summary.md, report.txt]`；三条回复都是 sender 真发出去并 `reply_verified` 的，不是我贴的模拟数据。

停滞回收也在真库上验过：插一条 `running` 且 `updated_at` 拨到 20 分钟前的任务 → 一个心跳（3s）后变
`open`、`attempts=1`；再拨两次 → `blocked`，`open_question='这个任务连续 3 轮没做完，要本人决定怎么继续'`。
自检用完即删（`tsk_stallcheck` 不是本人的活，不留）。

### 15.6 真机抓到的第二个坑：结案的任务把下一步吞了

第 3 步一开始没进账本：消费者在第 2 步就报了 `done`（它的判断没错，我事后又追加了要求），
第 3 步的消息于是撞上"结案任务不吃新步骤"的守卫——飞书里回复照常，`task show` 却只有 2 步。
这就是**账本和现实不一致**，比没有账本更糟：看起来有记录，记录却是错的。

修法：引用一个已 `done/failed/cancelled` 的任务说话 = 本人要接着做 → `set_task_state('open')` 重开同一本账，
步骤号接在后面（`test_quoting_a_finished_task_reopens_the_same_ledger`）。同一个活的步骤必须在一本账上，
不另开新任务；"结案任务不吃迟到事件"的守卫保留，它挡的是**旧消息的迟到回复**，不是本人新说的话。

### 15.7 与 L2 的边界

L1 只做"活做到哪一步"；`teachings.jsonl` 的检索、`turns.jsonl` 归因、`lesson` 候选、`复盘` 命令都属于 L2，仍是设计稿里的下一步。被推迟的 Codex 入口删除（到期日 2026-11-30）见 §14.6。

## 16. bridge 2.0 第三步：记忆检索（L2-a，2026-11-05）

### 16.1 问题

教材（`teachings.jsonl`）原来每轮**全量**塞进 prompt。教到几十条时预算就被吃光，更糟的是不相关的
旧条目会干扰判断：问一句天气，"预算口径以财务共享盘为准"也在 prompt 里。教材攒得越多，回答越糊。

### 16.2 做法（三层，各归各的）

| 层 | 位置 | 说明 |
|---|---|---|
| 真身 | `home/teachings.jsonl` | 只由 `bridge/teach.py` 读写，只追加 |
| 派生索引 | `home/memory.sqlite3` | FTS5；**可随时删**，下次检索自动重建 |
| 这一轮注入 | `payload.teachings` | `memory.retrieve()` 的输出，形状不变，所以六个渲染点一行没改 |

- 准则（principle/style/feedback + confidence ≥ 0.5）**必给**，最多 20 条；
- 资料（fact/pointer）拿消息正文检索，最多 8 条，排序 = IDF × 覆盖率 × 置信度 × 新近度；
- 预算仍是 4000 字，超了先砍资料再砍低置信度准则，`omitted` 如实报数；
- 查不到相关的就一条资料都不给（不做"象征性塞几条"）。

### 16.3 中文分词：为什么自己切

FTS5 默认分词器把一串汉字当成**一个** token：索引里是 `本月收入合计`，查询 `收入` 一个都命中不了。
`trigram` 分词器能解决子串，但依赖 SQLite 版本（写这一节时 Windows 侧 Python 自带的那份还没验证，设计稿 §8.3）。
所以自己切：汉字二元组（`本月收入合计` → `本月 月收 收入 入合 合计`）+ 英文数字整词。

> **后来核过了**（2026-11-05 19:21，Windows 真机）：Python 3.12.3 / sqlite 3.45.1 自带 `fts5=yes`、`trigram=yes`，
> `index_mode=fts5` 且中文命中，见 §18.7。自造分词的决定不变——行为由测试钉住，换机器不该换一套检索结果。没有 FTS5 模块的
SQLite 退化成普通表 + Python 扫描，`index_mode` 会如实报 `scan`（不是静默降级）。

### 16.4 判据（可复跑）

```bash
python3 -m pytest tests/test_memory.py -q     # 14 条：分词/必给准则/相关性/预算/禁用/200 条命中/索引重建/渲染
python3 -m pytest tests/test_teach.py -q      # entries() 暴露 status+confidence
python3 -m pytest -q                          # 全量（955 条）
```

关键几条：
- 200 条资料里问「今年的预算口径以哪里为准？」→ 只命中预算那条，发布流程那条不出现；
- 40 条长准则 + `max_chars=600` → 注入总量 ≤ 600，`omitted` 等于没进的条数（不偷偷溢出）；
- `rm memory.sqlite3` → 下次检索重建，`teach list` 条数不变；
- 索引文件写成垃圾 → 自动删掉重建，资料照样命中（**绝不把"索引坏了"当成"记忆丢了"**）；
- 新教一条之后再检索 → 索引失配自动刷新，新条目立刻可命中。

### 16.5 真机实录（2026-11-05 18:40，release `4e1e66d1022c623b`）

拿真 home 的教材副本（3 条真教材，另加两条演示资料，**不碰真库**）跑检索：

| 消息 | 注入的准则 | 注入的资料 | 用时 |
|---|---|---|---|
| 「今年的预算口径以哪里为准？」 | 2 条（全给） | 只给「预算口径以财务共享盘…」 | 3.8 ms |
| 「帮我把这句话翻成英文」 | 2 条（全给） | **0 条** | 0.6 ms |

真 home 上：`teach status` 的 `memory_index` = `{entries:0, records:3, index_mode:"idle", in_sync:true}`
（只有准则、没有资料 → 索引文件不存在是正常状态）；发一条 `问：把 17 和 25 相加，只回一个数字。`
走完整管线 → 回报 `42`，`reply_verified`（新 release 的 claim 检索路径在真机上是通的）。

演示过程中又抓到两个"同一份教材两种答案"的坑，都已修：
1. `status` 拿全部条目算指纹、`retrieve` 拿 active 条目建索引 → `in_sync` 永远是 false；
2. 索引把准则也算进去了 → 只有准则的机器上永远红着。现在 `memory.index_entries()` 是唯一口径
   （active 的 fact/pointer），`status`/`rebuild`/`retrieve` 共用。

### 16.6 顺带删掉的东西

`bridge/teach.py::payload_block`（"全量注入"的那块）已删除：同一件事（"这轮该给模型哪些教材"）
不允许有两个实现。两个 claim 入口（CLI 与 HTTP 网关）都改成 `memory.retrieve(home, 该事件的正文)`。

## 17. bridge 2.0 第四步：归因与复盘（L2-b，2026-11-05）

### 17.1 问题

"它为什么不聪明"这个问题，原来只能靠猜：回复发出去了、事件 done 了，但没人知道模型当时**看到的是
哪几条教材**、这一轮到底成功还是失败。没有归因，"学习闭环"就只是往教材里堆字——堆错了也不知道。

### 17.2 做法

| 层 | 是什么 | 谁写 |
|---|---|---|
| 原始事实 | `events.used_ids`（schema v6 新列）：**claim 那一刻**注入的教材 id | claim 路径（CLI + 网关） |
| 归因日志 | `home/turns.jsonl`：每轮 outcome/error/used_ids/task_id | 只有 `store`（事务提交后统一写） |
| 人工归纳 | 「复盘」→ 报告 + 最多一条候选教训 | 本人回「记」才落盘 |

三个刻意的选择：

1. **used_ids 存在事件行上**，不是回复时再查一遍检索。模型看到的是 claim 那一刻的教材；回复回来时
   教材可能已经变了，重查得到的是"事后真相"，那样的归因是假的。
2. **归因由 store 写，不由 CLI/消费者写**。事件终局（sent/failed/timeout/dead/尝试上限）只有 store 有
   权威答案；两处写同一份日志，将来必有一处漏掉某个路径（这正是 AGENTS §0「一条事实一个答案」）。
3. **复盘不自动写教材**。归纳是机械的（模板 + 计数 + 证据），判断是否成立是本人的事；自动写进去的
   "教训"一旦错了，会污染之后每一次回答，而且看起来像是本人教的。

### 17.3 教训是第四类教材

`kind=lesson`：参与检索（`SEARCH_KINDS = fact/pointer/lesson`）、expose 默认 `say`、渲染时单独一段
（`【上次这类事踩过的坑（教训）】`）。为什么不并进「资料」：资料是"只能自己知道的事实"，教训是
"以后遇到这类事怎么办"；混在一个标题下，模型会把教训当事实复述，或反过来把事实说成做法。

### 17.4 判据（可复跑）

```bash
python3 -m pytest tests/test_turns.py -q        # 19 条：日志本身 + store 三种终局的接线
python3 -m pytest tests/test_service.py -k Reflection -q   # 4 条：复盘 → 候选 → 「记」/「不记」
python3 -m pytest -q                            # 全量（985 条）
```

关键几条：
- 同一事件先 failed 再成功 → 归因里只剩一条 `sent`（不留误导性的旧失败）；
- 事务回滚 → 一条 turn 都不留（结局未成立）；
- 一次失败**不**编教训（一次算意外，两次才是模式）；两次同类失败 → 一条候选 + 两条证据（时间正序）；
- 「不记」之后 `teachings.jsonl` **bytes 不变**；「记」之后 `kind=lesson`、`expose=say`、pending 清空。

### 17.5 真机实录（2026-11-05 19:00，release `7b1dec74a1bc71a7`）

**归因**：发一条真实消息（`问：把 21 和 21 相加…`）走完整管线后：

```
$ feishu-bridge turns --limit 2
124  sent  consumer=dsh-main  used=['t-0003','t-0001']
123  sent  consumer=None      used=['t-0003','t-0001']
```

两条 `used_ids` 正是这条消息 claim 时注入的两条准则（`t-0003` 称呼宝哥、`t-0001` 信息库与权限）。
第一次部署时 `consumer` 漏记（`None`），真机一看就发现——补上后第二条立刻带上 `dsh-main`。
旧的那条保留原样：它记录的是当时的事实，不回头改写。

**复盘**：私聊里发「复盘」，回执原文（`im +messages-search` 取回）：

```
复盘（最近 1 轮）
· 答完 1 次、失败 0 次、超时 0 次
· 教材：平均每轮用 2.0 条

最近没有反复出现的失败模式，这次不编教训。
```

这次没有候选教训是**对的行为**（一次失败都没有，不该硬编一条），也说明"不猜"这条在真机上是通的。
候选 → 「记」→ 落成 `lesson` 的整串由 `tests/test_service.py::ReflectionTests` 四条钉住（含
「不记」之后教材日志 bytes 不变）。真机上还没造过一次重复失败，所以那一支只有单测覆盖，不写成已验证。

### 17.6 与"自训练"的边界

这套东西**不训练模型**，也不改任何 prompt 规则文件之外的参数。它能做到的只有：把"这轮发生了
什么"如实记下来、把重复的失败归纳成一条可读的做法、让本人一键决定它是否成为教材。教材下一轮
按相关性出现在 prompt 里——这就是当前环境（回合制、按事件起停）里"进化"的全部含义：改的是
**它被喂进去的上下文与做法**，不是权重。

## 18. Windows 侧上线 2.0：三处「看起来在保护」被真机打回（2026-11-05）

### 18.1 背景

Mac 侧 2.0 已闭环（§14–17，release `7b1dec74a1bc71a7`）。这次把同一份代码装到同事的 Windows
机器（Windows 11 家庭版 + WSL2 Ubuntu，家里网络走 tailnet），从 release `6ad0db440f34dee0`
升到 `7b1dec74a1bc71a7`。那台机器处于**有意挂起**（`hold.json` active），状态库还是 `user_version=3`。

三处缺陷都是在"部署 + 验收"这条路上被真机打出来的，共同点是：**判据本身在骗人**——升级/体检
会给出绿色结论，而机器其实是坏的或者跑的不是那份代码。

### 18.2 缺陷一：`verify.sh` 读错了 `hold.json` 的键名

`bridge/hold.py` 写的键是 `active`，`verify.sh` 读的是 `held`。挂起的机器上就变成：

```
VERIFY mode=held
VERIFY hold=inconsistent(file=running mode=held)
VERIFY RESULT=failed
```

F 阶段用 `verify.sh` 的退出码做结论（→ installer 返回 7），所以**这台挂起机器永远升不上去**，
而且看起来像"挂起状态坏了"，不是"断言写错了"。修：读 `active`；回归测试直接用真机那行字符串
钉住（`tests/test_windows_consumer_unit.py`）。

### 18.3 缺陷二：`check` 把 verify 的失败吞成绿灯

`tools/bridge-remote.sh check` 的远端命令是 `bash verify.sh | tail -n 25`——**管道的退出码是 tail 的 0**，
而脚本只 `exit $code`（doctor 的退出码）。真机实测：`VERIFY RESULT=failed`，`check` 退出码 `0`。
文档里写的是「check 退出码 0 才算成功」，于是这条门禁在 Windows 上从来没有真的生效过。

修：远端 `out="$(bash verify.sh 2>&1)"; vrc=$?` 捕获真实退出码、原样 `exit "$vrc"`；
`check` 只在 doctor 与 verify 都通过时返回 0。测试用**假 ssh** 跑真脚本
（`tests/test_bridge_remote_check.py`：verify 失败 → check rc=3）。

### 18.4 缺陷三：B 阶段是"合并"而不是"复制"，release id 两端不一致

`install-windows.ps1` 的 B 阶段原本是 `tar | tar -xf` 覆盖到 `~/feishu-personal-agent`。覆盖只加不删：
上游删掉的文件（`bridge/consumer.py`、`bridge/agent.py`、`cursor-consumer.py` …）会一直留在目标树里，
再被 `install.py` 原样复制进 `releases/<id>/`。后果是两件事同时坏：

1. release 里躺着死文件（§4 在 2026-11-03 手工清过一次，这次又长回来：`bridge/agent.py` 就是）；
2. release id 是 `bridge/*.py` 的哈希（`bridge/release.py`）——多一个旧文件，**同一份代码在两端算出
   两个 id**，AGENTS §2 的"两端对升级后跑的是哪份代码给同一个答案"直接不成立。

修：新增 `dist/wsl2/wsl/mirror-repo.py`（源树 → 目标树**镜像**：源树没有的文件删掉，保留目标树的
`.git/` 与 `__pycache__`/`.venv`/`node_modules` 构建缓存，源树不完整时 exit 9 且不碰目标）。
B 阶段改成"先 tar 到 ext4 暂存目录，再镜像落地"，打包时校验脚本在包里。

真机验收：升级后目标树 **159 个文件与发行包逐一对齐（diff 0 行）**，`bridge/agent.py` 消失，
`release_match=ok`。

### 18.5 一次操作事故：改 `.ps1` 掉了 BOM，升级在解析阶段就失败

第一次跑 upgrade 直接失败：

```
+ [string[]]$ArgumentList = @(),
~  在 ... 中缺少表达式。
ParserError: MissingExpressionAfterToken
```

原因不是逻辑，是**编码**：我用编辑器改 `install-windows.ps1` 时把它写成了"无 BOM 的 UTF-8 + 纯 LF"。
PowerShell 5.1 对无 BOM 文件按 ANSI(CP936) 解码，中文注释把引号/括号吞掉，整脚本语法错误。
2026-11-04 那个能装上的包里，这个文件是 **BOM + CRLF**（`git show` 里 blob 也是带 BOM 的）。

修：恢复 BOM + CRLF；并在 `pack-windows.sh` 里加打包门禁「含非 ASCII 的 `.ps1` 必须带 UTF-8 BOM」，
另加一条 macOS 上就能跑的 pytest（`tests/test_pack_windows.py`）——原来这条只写在 Windows 的
`WindowsDistribution.Tests.ps1` 里，Mac 侧打包时拦不住。

### 18.6 判据（可复跑）

```bash
python3 -m pytest -q                                    # 993 passed
python3 -m pytest tests/test_windows_repo_mirror.py tests/test_bridge_remote_check.py -q
bash dist/wsl2/pack-windows.sh                          # 166 个文件，含 BOM/镜像门禁
tools/bridge-remote.sh --host <magicdns> --user <user> upgrade dist/windows/feishu-bridge-windows-<日期>.zip
tools/bridge-remote.sh --host <magicdns> --user <user> check   # 退出码 0 = WSL 里 doctor --assert + verify.sh 全过
```

### 18.7 真机实录（2026-11-05 19:21，同事机器，release `7b1dec74a1bc71a7`）

- `version`：`installed_release == running_release == 7b1dec74a1bc71a7`，`update_pending=false`
  （与 Mac 侧 §17 的真机 release 是同一个 id——两端一个答案）。
- `check` 退出码 `0`，`verify.sh` 全绿：`hold=dropped=0`、`release_match=ok`、`consumer_release=ok`、
  `legacy_units=0`、`RESULT=ok`。
- 消费者真的换了代码：`ActiveEnterTimestamp` 从 `2026-11-04 22:22:17` → `2026-11-05 19:21:34`，
  `ps -o args=` 是 `/home/<user>/.feishu-personal-agent/current/dsh-consumer.py`（不是 unit 文件内容）。
- 迁移：`user_version 3 → 6`；`events.used_ids` 建出；`tasks`/`task_messages` 建出；
  inbox/outbox/events/consumers = 120/54/103/3 行数与状态分布不变。D 阶段 `MIGRATE clean_count=0`。
- FTS5（这次专门核的）：Python 3.12.3 / sqlite 3.45.1，`fts5=yes`、`trigram=yes`；
  `bridge.memory.retrieve('这个月收入多少？')` → `index_mode=fts5`，命中「本月收入合计 12 万」。
  即 Windows 侧自带 CJK 二元分词工作，不依赖 trigram。
- 回滚路径（**不是"再 upgrade 一次"就完事**）：`releases/6ad0db440f34dee0` 与 2026-11-04 的发行包都在，
  但库已经就地迁到 v6，而旧代码的门禁是 `if version > 3: raise RuntimeError`（`5278569:bridge/store.py:46`）。
  所以回滚顺序是：挂起 → 停两个 unit → 恢复升级前的 `state.sqlite3` 快照（升级前那次 sqlite backup，
  不是 `cp` 一个 WAL 态文件）→ 装旧包 → 起。只切 release 不换库 = `Restart=always` 崩溃循环。
  步骤写在 `docs/ops-remote-manage.md` §4，本节只记这条边界为什么存在。
- 遗留（升级前就有，不阻塞）：holder 报「2 个 wsl.exe 客户端」；`events_dead=20` 只是 doctor 的 warn。

### 18.7.1 Windows 上**没有**跑过的支路（别当成已验证）

真机复核覆盖了：升级/迁移、`verify.sh`、PS 测试套件、FTS5 与中文检索、真实消息闭环、多轮任务账本。
以下三支**只在 Mac 与 WSL2 上跑过**，Windows 真机尚未实测，按「没验过」对待：

- 「复盘」指令（L2-b）：`feishu-bridge turns` 看过（真机 `outcome=sent`），但没在 Windows 上发过「复盘」、
  也没走过「候选教训 → 记」；
- 教学通道的写入路径：真机测试时是临时 `teach enable`、测完删掉 `teach.json`，长期开启的状态没跑过；
- 挂起/恢复（hold/release）之外的控制指令：`下班/上班` 自动命令通道没在 Windows 上验过。

想补的时候按 §13 的远程只读体检走，别在同事工作时间窗里动生产。

### 18.8 两套 PowerShell 测试第一次在 Windows 上真跑（296 条）

`tools/bridge-remote.sh ps-tests` 在真机上跑发行包自带的 `dist/wsl2/tests/`：

```
PASSED WindowsDistribution.Tests.ps1（186 项 ok）
PASSED InstallWindows.Tests.ps1（110 项 ok）
```

第一遍是红的，而且**红得对**：`upgrade.sh 不再需要 cursor 兼容层` 这条用
`-notmatch 'CURSOR_BINARY'` 判整个文件，而 `upgrade.sh` 的注释里正好在解释这层兼容为什么被删
（L0 提交 `935c956` 加的）——把"代码里有残留"和"注释里提过这个词"判成了同一件事。改成先剥掉
`^\s*#` 注释行再判。这个假红在 macOS 上永远看不到（没有 pwsh），只能靠真机这套测试。

顺带确认：这次改的 B 阶段（暂存 + `mirror-repo.py` 镜像）在 Windows PowerShell 5.1 下语法与行为
都过，`InstallWindows.Tests.ps1` 110 条包含编排顺序、B 阶段失败语义、`/etc/wsl.conf` 写入意图。

### 18.9 幂等重跑实录

同一台机器又跑了两遍 upgrade（第 2 遍是干净工作树打的包，第 3 遍带 ps-tests 修复）：

- `release_id` 不变（`7b1dec74a1bc71a7`），`pruned_releases=[]`（没有可剪的旧 release）；
- D 阶段 `RELOAD_CONSUMER=active 变化 19:21:34 → 19:26:49 → 19:28:23`（每次重跑都重载消费者）；
- `MIGRATE clean_count=0`（第二次起没有可迁移的旧 unit）；
- F 阶段每次 21 行全绿、退出码 `0`。

"升级没生效"时首选补救就是这条：包没变也再跑一次，看 `check` 与 `reload` 证据。

- 真机消息闭环：本节写的时候还没做（机器有意挂起），后来补做了，见 §19。

### 19. 真机消息闭环（2026-11-05 19:40–19:45，Windows 唯一真机）

目标只有一句：升完 2.0 之后**到底能不能真回话**。机器是有意 `held` 的，为了不打扰同事，只在一个
2 人自测群里 @ 一次（`bridge 自测（可在飞书里直接解散）`，成员只有两位，没有第三个人看到）。

实录（时间取自 `bridge.log` / `status.json` / `outbox` / `turns.jsonl`，不是"我记得"）：

| 时刻 | 事实 |
|---|---|
| 19:40:21 | release hold（直接写 `hold.json`，避开 CLI 在她自聊里留"上班"回执） |
| 19:40:27 | @ 消息 1 发出 |
| 19:40:46 | `search live hits=1`（窗口恢复成 86400000ms 后第一次命中） |
| 19:40:50 | inbox 新增一行、`events.id=121` |
| 19:40:52 | 消费者 `dsh-main` 认领 |
| 19:41:03 | **我错误地把 hold 又打开**（窗口只留 13 秒）→ 事件 `state=dead`、`error='Bridge held'`，`turns` 记 `AgentFailed` |
| 19:42:18 | @ 消息 2 发出 |
| 19:44:46 | outbox `first_send_at`（回复已发） |
| 19:44:48 | outbox `state=reply_verified`，`turns`：`outcome=sent`、`consumer=dsh-main`、`event_id=122` |
| 19:44:5x | 飞书里核对：回复的 `reply_to` 就是那条测试消息，正文以「我是机主的 AI 助手……来自机主的AI助手」结尾 |

第一轮失败**是我的测试窗口太短，不是桥的缺陷**：`held` 的语义就是"不回复"（`docs/bridge-hold-switch-requirements.md`
§13-15），把待回事件判 dead 是正确行为，不会补发。端到端 ≈ 2 分 28 秒，绝大部分是消费者对 hold
的轮询粒度（约 70 秒）+ DSH 单轮耗时，不是桥在排队。

两条结构性事实（以后别再试错）：

1. **别人发给机主的私聊不会被采集**。`transport.search()` 不带 `chat_id` 时固定
   `chat_type=group + is_at_me`；p2p 只在**已登记**的自聊/教学会话里被单独扫。所以拿私聊做
   端到端测试，会看到"消息确实到了飞书、但 inbox 一行不涨"——那不是采集坏了。派活/提问走
   群里 @，或者走自聊控制通道。
2. **关窗前必须等 outbox 到 `reply_verified`**。中途挂起＝丢弃，事件进 dead，`hold engaged ...
   dropped=N` 会如实计数（本次 dropped=1）。

收尾：机器已恢复 `mode=held`（`by=mac-live-test`、`reason=Mac 侧真机测试结束，恢复原挂起`），
`check` 退出码 `0`、`verify.sh` 全绿；测试痕迹只剩测试群里那两条 @ 与一条回复。

### 20. 真机多轮任务（L1 账本）实录（2026-11-05 19:50–19:56，同一台 Windows 真机）

单轮问答通了不等于账本通了。这一轮专门验"一个活分两步"：派活建账 → 第一步答复 → **引用回复**续做 →
第二步答复，全部在真机上、用真实飞书消息。

前置条件（这次才发现的）：`做：`/`干活：`/`安排：` 只在**教学通道**里被识别
（`service.route_self_chat` 第一行就是 `teach_log.enabled_chat(home)`）。那台机器 `teach.json`
根本不存在（教学从未启用），所以派活必须先 `feishu-bridge teach enable --chat <本人私聊 oc_…>`
（会校验 `chat_mode == 'p2p'`）。测试完 `teach disable` 并删掉 `teach.json`，恢复成"从未启用"。

第一遍（19:50–19:52）记账成功但**飞书里只看到第一条回复**：

```
19:52:37 WARNING sender TransportError: lookup open.feishu.cn ...: i/o timeout
19:52:54 INFO hold engaged ... dropped=1
```

第二轮答复已经在账本里（`steps[1].outcome=sent`、`turns` 也记了 `sent`），但它进 outbox 后
还没发出去就赶上我把挂起打开，被当成"排队中的消息"作废了。三条事实：

1. `turns.outcome=sent` 的语义是"**消费者交出了答复**"，不是"飞书里收到了"；后者只看
   `outbox.state`（`reply_verified` 才算落地）。
2. 挂起会作废排队中的回复（`dropped=N` 会如实计数）——**关窗前必须等 outbox 到 `reply_verified`**，
   和 §19 那条是同一个坑，这次只是换了个触发原因（DNS 抖动 + 手快）。
3. 消费者的账本回填和回复写在同一个事务里，所以"账有、回复没发出去"这种半截状态不会出现；
   出现的是"账有、回复被作废"，这是有意的丢弃语义。

第二遍（19:54:00–19:56:23）干净跑通，判据全部落在真机上：

| 判据 | 真机结果 |
|---|---|
| 建账 | `tasks` 新增 `tsk_26e0b5e67cfd`，标题取正文，`state` 从 open → done |
| 第一步 | `steps[0]`：`outcome=sent`，note 是消费者写的实测结论（python 3.12.3 / sqlite 3.45.1 / FTS5 支持）；outbox `reply_verified`（reply_id `om_…755a21`） |
| 归属 | `task_messages` 4 行、2 个任务各 2 行；第二条消息 `parent_id=root_id=第一条` |
| 引用续做 | 第二条答复挂到**同一条任务**（不是新开账），`steps[1]` 的 note 也写进去了 |
| 归因 | `turns` 两条：`event=125/126`，`task_id=tsk_26e0b5e67cfd` |
| 飞书侧 | 自聊里能看到"派活 → 答复 → 引用续做 → 答复"四段，`reply_to` 都对得上 |
| 副作用 | 窗口内另有 1 条无关消息被采集后判 `ignored`（没回、没进账）；库计数 inbox/outbox/events 只增了我这两条与一声明 |

收尾：机器恢复 `mode=held`；`teach.json` 已删回"未启用"；两个测试任务一个 `cancelled`
（第一遍那个）、一个 `done`（第二遍那个，两步都在）。证据在 `coordination/windows-2.0-evidence/33…42`。

### 21. 验收侧「同一事实两个答案」收口（2026-11-05，清单 §10 剩余 4 项）

这四项都不是运行缺陷，是**同一件事在代码和文档里各有一个答案**。按最新代码对齐，不留注释分叉。

**① F 阶段 vs `-Mode Check` 对 `sending_enabled`（清单 §10-②）**

`verify.sh` 判 `bad`（rc=3）是对的：新装默认 `false`（`install.py` 里 `setdefault`），
装完不 `resume` 就永远不回复，这必须让验收红。`-Mode Check` 原来只 `Write-Warn`，
于是同一台机器换条通道就得出"装好了"。改成 `Write-Err` + `$problems++`，
文案改成"已安装但没 resume：`feishu-bridge resume` 后重跑"。

**② worker 名单：verifier 自己抄了一份（清单 §10-⑥）**

事实收敛到 `bridge/service.py:WORKER_NAMES`（`run()` 用它起线程）。`verify.sh` 不再写
`('collector', 'details', 'sender')`，改成从目标树（`~/feishu-personal-agent`，退而本脚本同树的
仓库根）`import bridge.service` 读名单，按"缺 / 死 / 多"给明细：

```
VERIFY workers=ok
VERIFY workers=missing=sender        # bad → rc=3
VERIFY workers=extra=analyst         # status 里的 worker 不是这份代码的
VERIFY workers=ok(fallback)          # 拿不到树，退到"报了谁是活的都得活"（不静默）
```

"多"也要报：`status.json` 里出现当前代码没有的 worker，说明跑的不是这份代码。
真机教训就是旧包的 4 worker 验新代码假红了——verifier 抄名单，等于给同一事实留了第二个答案。

**③ schema 版本三处说法（清单 §10-⑦）**

`CONTRACTS.md` 统一到 **v6**（`bridge/store.py:SCHEMA_VERSION`），并把迁移链写成代码里的四步：
v1/v2→v3 补表/列（含 `claim_token`、`verify_attempts`，无令牌租约回 pending）→ v3→v4
`analysis_completed` 改名 `reply_queued` → v4→v5 建 `tasks`/`task_messages` → v5→v6 加
`events.used_ids`；更高版本在任何写入前拒绝。`CONSUMER-TEMPLATE.md`、`INSTALL-MACOS.md`
里残留的 "schema v3" 一并改掉，回滚那句改成"跨 schema 回滚要连库一起退"。

**④ `state.sqlite3` "不会被覆盖"（清单 §10-⑧）**

字面成立（不替换文件），但**会被就地迁移到当前 schema**（本次 v3→v6），只有单向、没有 down
脚本，升级后旧代码打不开这个库。`docs/ops-remote-manage.md` §3 改成明说这一点并指回 §4 的
回滚顺序（先退库、再退代码），别让"升级不动库"变成回滚依据。

**护栏**（缺陷会随下一个 release 重新长出来的地方都钉住了）：

| 位置 | 断言 |
|---|---|
| `tests/test_windows_consumer_unit.py` | verify.sh 读 `WORKER_NAMES`、不写死名单；真跑 verify.sh：缺/多/正好三种结论，含 `ok(fallback)` 不是 ok |
| `tests/test_pack_windows.py` | 打包口子拒绝"verify.sh 里写死 worker 名单"的包；`-Mode Check` 对 `sending_enabled=false` 必须 `Write-Err`+`$problems++`，旧警告文案必须消失 |
| `dist/wsl2/pack-windows.sh` | 出包前 grep 门禁（同上第一条） |
| `dist/wsl2/tests/WindowsDistribution.Tests.ps1` | 真机侧再断言一次（包内文件） |

副作用：`bridge/service.py` 变了，release id 从 `7b1dec74a1bc71a7` 变成 `b30f577f88e89aa5`；
为了让"升级后跑的哪份代码"两端仍是一个答案，重新打包并在真机升级到该 id 后再跑 check。

**真机复核（2026-11-05 20:07–20:10，同一台 Windows 真机）**

改完必须落到那台机器上，否则"两端一个答案"只是本地说法：

| 判据 | 输出 |
|---|---|
| `upgrade <zip>` | rc=0；F 阶段 `workers = ok`（名单是现场从 `bridge/service.py` 读的）、`release = b30f577f88e89aa5`、`release_match = ok`、`hold=dropped = 0`、`consumer_release = ok` |
| `check` | rc=0，`RESULT = ok`，`mode = held`（她下班挂起状态原样，没被这次升级动过） |
| `ps-tests` | `WindowsDistribution.Tests.ps1` 190 项 ok、`InstallWindows.Tests.ps1` 110 项 ok |

`ps-tests` 第一遍红了一处，正好是同一类毛病：PowerShell 套件里还留着
`Assert-True ($verify -match "collector") '断言四个 worker'`——夹具自己也抄了那份名单。
改成断言 `emit workers|bad workers`（"体检会检查 worker 存活"）后全绿。**门禁自己也是代码，
同样会留下第二个答案。**

release id 因此从 `7b1dec74a1bc71a7` 变成 `b30f577f88e89aa5`；真机已升级到该 id，
两端（Mac 工作树 / 真机运行）再次一致。

**收口时又抓到三处"同一份名单"（同一事实的第二、三、四份）**

改完上面四处再 grep 一遍，worker 名单还被抄在三个地方，全部改成 import：

| 位置 | 原来的写法 | 后果 |
|---|---|---|
| `bridge/cli.py` 的 `_doctor_checks` | `for name in ('collector', 'details', 'sender')` | worker 一增减，`doctor` 就把健康机器报成「缺失=sender」，而 `doctor` 正是人和自动化最常看的那一眼（延迟导入 `from .service import WORKER_NAMES`） |
| `dist/wsl2/setup.sh` 第 8 步验收 | 同上 | C 阶段装完的验收会把正常机器判成失败；改成从 `$SRC`（仓库根）import，读不到就**硬失败**并说清下一步，不静默降级 |
| `dist/wsl2/tests/WindowsDistribution.Tests.ps1` | `Assert-True ($verify -match "collector")` | 门禁自己抄名单：verify.sh 一改它就红（ps-tests 第一遍就是这么红的） |

零残留判据（`tests/test_windows_consumer_unit.py::test_worker_list_is_not_copied_anywhere_in_runtime_code`）：
`bridge/*.py` + `verify.sh` + `setup.sh` + `install-windows.ps1` 里除 `service.py` 的定义行外，
不许再出现这份字面量。

`bridge/cli.py` 变了 → release id 再变：`b30f577f88e89aa5` → **`cf721a642a6cdcba`**。真机已升级并复核：
`upgrade` rc=0（F 阶段 `workers=ok`、`release_match=ok`、`hold=dropped=0`、`consumer_release=ok`）、
`check` rc=0 `RESULT=ok` `mode=held`、`ps-tests` 190+110 项 ok、`doctor --assert` rc=0 且
`workers_complete` 在场（名单是现场从代码读的）。

### 22. 收尾清理：把"以后会咬人"的说法一次扫干净（2026-11-05 20:15–20:22）

§21 收口的是"同一事实两个答案"，这一轮把**顺着它能摸到的所有残留**扫掉。判据都可复跑（见下）。

| 类别 | 原来 | 现在 |
|---|---|---|
| `check` 到底跑什么 | `docs/ops-remote-manage.md`「两端都跑 doctor --assert」 | Windows 宿主没有 `doctor`：写清 `check` = WSL 里 `doctor --assert`（失败 5s 重试）+ WSL 的 `verify.sh`，退出码取大；宿主侧对等物是 `-Mode Check`。`docs/windows-fleet-rollout.md` 两处同样改掉 |
| 发行包机器的升级路径 | `dist/wsl2/README.md`、`docs/onboard-windows-dsh-colleague.md` 都留着 `git pull && python3 install.py` | 明确「解压快照没有 `.git`，这条路不存在」，升级走 `bridge-remote.sh … upgrade <zip>`；`git clone` 机器才谈心 pull，且托管阶段由 D 阶段负责（手工路径只对 git clone 成立） |
| 回滚 | 同事向文档「重发上一版 zip 再 upgrade 即可」；`feishu-bridge rollback` 帮助文字「schema 兼容性自负」 | 两处都改成「先退库再退代码」：upgrade 会就地迁移 `state.sqlite3` 且无 down 脚本，旧代码打不开新库（`RuntimeError` → `Restart=always` 崩溃循环）。help 里直接指到 `docs/ops-remote-manage.md` §4 |
| worker 数量/名字 | README、CONTRACTS、INSTALL-MACOS、INSTALL-WINDOWS、fleet 文档、包内 README、安装器阶段说明里散着「三个 worker」「四个 worker」「四 worker 验收」 | 全部改成「worker 全活（名单见 `bridge/service.py:WORKER_NAMES`）」，一个数字都不留在正文里 |

**可复跑的零残留判据**（输出在 `coordination/windows-2.0-evidence/53-consistency-scan.txt`）：

```bash
# 1) 名单字面量只剩定义处 + 打包门禁的"禁止模式"
grep -rn "('collector', 'details', 'sender')" bridge dist/wsl2 --include="*.py" --include="*.sh" --include="*.ps1"
#    → bridge/service.py、dist/wsl2/pack-windows.sh（门禁自己）
# 2) 文档里不再出现 worker 数量
grep -rn "三个 worker\|四个 worker\|四 worker" README.md CONTRACTS.md INSTALL-*.md docs/*.md dist/wsl2/README.md
#    → 空
# 3) schema 版本说法只有 v6
grep -rn "schema v[0-9]\|user_version=[0-9]" README.md CONTRACTS.md INSTALL-MACOS.md CONSUMER-TEMPLATE.md docs/*.md
# 4) Windows 文档里的 git pull 只出现在"没有这条路"的说明里
# 5) 「两端 doctor」：空
```

release id 因 `bridge/cli.py`（回滚帮助文案）再变：`cf721a642a6cdcba` → **`dd97b48161861f94`**。
真机已升级并复核：`upgrade` rc=0（F 阶段全 ok）、`check` rc=0 `RESULT=ok` `mode=held`、
`ps-tests` 190+110 项 ok；机器回到她原来的挂起状态，没被这轮清理改动。

## 22. bridge 2.0 第五步：感知延迟（L3，2026-11-05）

### 22.1 问题

"它回得太慢"这个感受一直存在，但没人能量化。设计 L3 时先去测，测完发现**原本计划做的「内置分析器」
根本不对症**——延迟不在"该不该回"的判断上，而在两个工程细节上。

### 22.2 实测：一条消息的三段耗时

新增 `feishu-bridge latency`（`bridge/metrics.py`）之前，这些数只能手写 SQL 查四张表。改前改后都在
同一台机器上量：

| 段 | 改前 p50 | 改后 p50（两次采样） | 说明 |
|---|---|---|---|
| 采集（发出 → 采集到） | 26.9s | **11.1s / 10.1s** | 已贴住 11.1s 的索引下限 |
| 消费者（采集到 → 答复生成） | 14.9s | 13.9s / 16.7s | 模型单轮 7～12s + 轮询 5s，波动来自模型 |
| 投递（生成 → 飞书读回） | 4.6s | 4.3s / 4.9s | 已是两跳 API 的自然成本 |
| **合计** | **54.6s** | **30.6s / 33.8s** | 感知延迟降 38～44% |

两次采样都贴出来，不挑好看的那个：消费者那段的波动（13.9 vs 16.7）是模型单轮的真实抖动，
不是改动引起的；采集那段两次都在索引下限附近，说明瓶颈已经换人了。

### 22.3 两个真正的瓶颈（都不是"模型慢"）

**一、客户端自己写死的 10 秒搜索节流。**
`Transport._pace(search=True)` 里 `next_search = now + 10`——每次搜索之间强制等 10 秒。搜索接口本身
实测只要 0.6～1.7 秒，所以真实节奏 = 10 秒节流 + 接口耗时，**与 `live_interval_seconds` 无关**：
把它从 12 调到 3，日志里的扫描间隔纹丝不动（14s）。

先验证再改：连发 10 次搜索、间隔 1 秒，全部成功，无 230020（脚本输出见本节末）。于是把节流改成
`search_min_interval_seconds`（默认 1.0，可按机器覆盖），并把"限流"的唯一权威交回服务端：
真限流时 230020 会让传输层冻结 60 秒，这个处理本来就有，不需要第二道保险。
改完：`search live` 间隔 14s → **5s**，`search self` 34s → 12s。

**二、飞书搜索索引的可见延迟 = 11.1 秒。**
这是改完节流后仍存在的下限。实测方法：发一条新消息，然后每秒用同样的搜索条件查一次，第 11 次才查到。
**这不是我们能改的部分**——搜索接口是最终一致的，采集延迟的下限由服务端索引决定。
这也解释了为什么之前 27s 里那 14s 的轮询间隔一去，采集就掉到 11s：剩下的正好是索引延迟。

### 22.4 为什么不做「内置分析器」

原计划 L3 是"在 bridge 里加本地分析器，判要不要回/该谁办，省模型调用"。数据否掉了它：

1. **省不出延迟**：延迟=11s 索引 + 12s 模型；省掉模型那 12s 只对"本来就不该回"的消息有效，
   用户感知不到；
2. **判定已存在**：`inbox` 156 条里 33 条根本没起模型（15 条没 @、6 条教材私聊、3 条自己的、
   9 条人工），admission 已经在做同一件事，再加一层 = 同一事实两个答案；
3. **最坏是静默吞消息**：AGENTS §1 的红线。

结论：不做。若将来要做，前置条件是先拿到"大量起了模型但其实不需要"的样本——现在是 0。

### 22.5 质量没有被牺牲（这是本轮的前提）

用户的要求是"提速但不能降低回复质量"。所以本轮只动**客户端等待**，不动检索、模型、prompt、
投递校验任何一环。判据：

- 教材注入：改后 5 轮平均 2.0 条（改前 2.0 条），`used_ids` 仍是 `t-0003 / t-0001`；
- 投递：新消息全部 `reply_verified`，无一条 `needs_attention`；
- 失败：`turns` 里最近 5 轮全 `sent`，`failed/timeout = 0`；
- 限流：检查窗口内 0 条 230020；
- 回复内容：抽看真机答复仍是完整回答 + 签名，没有被截断或变短（"只回一个数字"的抽查就是 `18`）。

**明确没做的事**（都会拿质量换速度）：换更小的模型、裁 prompt、减少教材注入、跳过投递校验、
用模板或缓存直接作答。

### 22.6 判据（可复跑）

```bash
feishu-bridge latency --window 8            # 三段分位数 + 间隔 + 积压数
python3 -m pytest tests/test_latency.py -q  # 9 条：口径（nearest-rank/缺 verified/负区间/空库/窗口/积压）
python3 -m pytest tests/test_transport.py -k SearchPacing -q   # 节流可配 + 真用时间量
python3 -m pytest tests/test_install.py -k Agreement -q        # install 默认值与 service fallback 不许漂移
/tmp/burst_search.sh                        # 连发 10 次搜索验证不限流（输出：10 次全 ok，0.6~1.7s）
python3 /tmp/index_lag.py                   # 索引可见延迟（输出：11.1 秒）
```

### 22.7 收口决定（2026-11-05 本人拍板）

**「先回执后答复」不做。** 本人结论：现有 30 秒左右可以接受，且没有别的优化方法。

理由记在这里，免得以后有人重新提：剩下的 31 秒里，11 秒是飞书搜索索引的可见延迟（服务端最终一致，
改不了）、约 14 秒是模型单轮思考（换小模型或用模板就会掉质量）、约 5 秒是两跳 API 的投递确认。
要再压只能"先回一句话再给真答案"，而那要改不可变 outbox 的主键（一条消息只能一条回复）——
用一个数据模型风险换体感，在已经可接受的情况下不划算。

**消费者分阶段时间戳也没加。** 它本来用于定位"claim 到起模型之间那段黑盒"，但瓶颈已经定位完了
（索引延迟 + 模型单轮 + 投递），加时间戳对当前决策没有影响；等下次真要抠消费者那 14 秒时再补。

### 22.7.1 当初列的杠杆（供将来参考）

| 想法 | 能省 | 为什么这轮不做 |
|---|---|---|
| 消费者轮询 5s → 1s | ~2s | 收益小，且每秒一次 CLI 子进程在笔记本上不划算 |
| 「先回执后答复」 | 感知从 ~30s 到 ~15s | 要动不可变 outbox 主键（一条消息只能一条回复），需本人拍板 |
| 事件流采集 | 采集 11s → <1s | `lark-cli event list` 里 IM 消息事件只有 **bot** 身份，个人号桥接拿不到；这条路在当前架构下不通 |

---

## 23. 同事 Mac 从 1.x 升到 2.0：v3 库就地迁到 v6，agentkit 消费者要手动换 release（2026-11-05 21:1x）

**背景**：同事的 Mac（macOS 13.3 / Apple M1，tailnet 里那台 `<同事机>`）之前装的是 1.x 时代的
release `76ab7ce39835e04b`（桥里还有内置大脑，外部消费者是 `agentkit-dsh`），库是 `user_version=3`。
这次按 2.0 现状（§14–17、§22）升到 `de65090fb8580c03`。**这台机器当晚不在同一局域网**：
`<同事机内网 IP>` ping 不通、SSH 超时，改走 tailnet `<同事机>.<tailnet>` 完成的（同一台机器，
只是路由不同——ops 文档里"内网 IP"不是唯一入口）。

**可复跑的步骤**（和 Windows 的 §18 一一对应，只是换 macOS 的入口）：

```bash
# 1) 升级前先留回滚材料：库要迁到 v6，旧 release 打不开新库
sqlite3 "$H/state.sqlite3" ".backup '$H/pre-2.0-state-$(date +%Y%m%d-%H%M%S).sqlite3'"
# 2) 源码镜像到目标树（不是覆盖，见 §18.4 的教训）
tar xzf feishu-2.0.tgz -C /tmp/src-2.0 && rsync -a --delete /tmp/src-2.0/ ~/feishu-personal-agent/
# 3) 装（沿用原 agentkit consumer 名）+ 自动激活 + 体检
python3 install.py --with-agentkit --agentkit-agent dsh --agentkit-consumer agentkit-dsh --verify
# 4) 消费者不跟 current：必须显式重载，并用 ps 看它实际跑哪个 release
python3 "$H/current/agentkit-consumerctl.py" restart
```

**真机数字（21:11–21:15）**：

- `release_match=ok`：`installed == running == de65090fb8580c03`，`update_pending=false`；`doctor --assert` 退出码 `0`、`verdict=ok`、无 fail/warn。
- 迁移：`user_version 3 → 6`；events/inbox/outbox/consumers = **5/7/4/1 行数与升级前一致**；
  `tasks`/`task_messages` 建出；`ENABLE_FTS5` 在，实测 `Index.mode=fts5`、
  `tokens('本月收入')` 能命中「本月收入合计 12 万」——CJK 二元分词在 macOS 的 Python 3.9.6 / sqlite 3.54 上也工作。
- 消费者：`ps -o args=` 从 `releases/76ab7ce39835e04b/...` 换成 `releases/de65090fb8580c03/...`；
  `consumer status` 里 `agentkit-dsh live=true`，`last_seen` 秒级新鲜；`workers` 只剩 3 个（collector/details/sender，analyst 随内置大脑一起删了）。
- 2.0 的 `latency`：5 个样本 answered、`p50 37.1s / p90 60.5s`，`pending=0 deliver_missing=0 negatives=0`。
- 真机 @ 验收：群里发一条（`om_x100b61fc413778a0dd64a4e4f3858f6`），她回
  `om_x100b61fc5ce7513cc13a5936217026c`（`sender_type=user`、`reply_to` 正确），正文自称与署名都带她的名字
  （「我是<本人>的 AI 助手」/「此回复信息来自<本人>的 AI 助手」）；event 7 `done`、outbox `reply_verified`。

**2.0 在真机上暴露的两个"提示语"问题（不是故障，但会误导下一个人）**：

1. 安装器打完会警告「本次安装没有装/重载任何事件消费者……下一步 `--with-dsh-consumer`」——
   这台机器的 `agentkit-dsh` 明明还在 claim 事件（`consumer_offline=ok`）。warning 想说的是
   "我这次没帮你重载消费者"，但对已经存在的消费者会被读成"你现在没有大脑"。
2. `external.json` 被收敛成 `{"reply_timeout_seconds": 600}`（`enabled` 开关已删除）。升级前
   `enabled: true` 的那份文件在真机上被就地改写——这是 2.0 的设计（不再有"外部消费者总开关"），
   但升级前如果有人靠它下过"关掉外部消费者"的判断，升级后就失效了。

**回滚边界（这台机器现在的真实情况）**：旧 release `76ab7ce39835e04b` 还在 `releases/` 里，但库已经
迁到 v6，旧代码的门禁是 `user_version > 3 → RuntimeError`。所以回滚顺序是：停 bridge 与消费者 →
恢复 `pre-2.0-state-20261105-211205.sqlite3` 快照 → 装旧包 → 起。**只切 release 不换库 = 崩溃循环**。

**遗留（不影响判据）**：`consumers` 表里 `agentkit-dsh` 的 `fails=5` 是升级前（当天 15:00–15:29 调试期）
累计的历史计数，事件表 6 条全 `done`，升级后到现在没有新的 fail。

## 24. 求助把私聊锁死了一夜（2026-11-05 晚真机事故，2026-11-06 已修）

### 24.1 他看到的（原文摘录，含群里的第三人消息）

| 时间 | 他说/他发的 | 桥的回应 |
|---|---|---|
| 20:50 | 群 `oc_99…` 里有人只发了一个「14」 | 私聊收到【需要你出手】…选项 A/B/C |
| 20:52 | 群里有人只发了一个「18」 | 私聊又收到一条【需要你出手】 |
| 20:55 | 「问：把 33 和 44 相加」 | **「我有 2 个问题在等你，这条对不上号。请用『引用回复』…」** |
| 20:56 | 「问：把 55 和 66 相加」 | 同上，逐字重复 |
| 22:03 | 「帮我打开我系统的 UU APP」 | 同上，逐字重复 |
| 22:05 | 「你傻啦」 | 同上，逐字重复 |
| 22:09 | 「你傻啦」 | 同上，逐字重复 |
| 22:10 | 「你真傻了」 | 同上，逐字重复 |

从 20:55 到 22:10，他没有一条消息被真正处理过；两条求助一直挂着，晚上就这么过去了。

### 24.2 根因：一句"当答复处理"把消息吃掉

`Service.try_answer_question` 里的分支：待答求助多于一条、而这条消息又没引用时，
回一句「对不上号」然后 `return True`——`True` 的语义是"已处理，不要产生事件"。
于是**消息既没落教材、也没进事件管道，直接消失**。同一段还有个更隐蔽的：

```python
else:
    matched = pending[0]   # 只有一条待答 → 任何私聊正文都当答复
```

只有一个求助挂着时，他发「帮我打开 UU APP」会被当成那条求助的答复写进教材，
再拿去重答原消息——错得更离谱，只是没人当场看见。

两件事叠加才成了事故：
1. 求助**永不过期**（只有"要不要记"的单槽候选有 TTL，求助没有），挂一夜就锁一夜；
2. 拒绝的话**逐字重复**，且不区分"这是新的指令"和"这是对问题的回答"。

### 24.3 改法（删除优先）

- **删除**"没引用时猜一个 / 没引用就拒绝"这两条分支。答复只认**引用回复**——
  那是唯一的结构性信号，猜正文一定会在某天把一句骂人话写成教材（当晚「你傻啦」就在候选里）。
- **没引用的消息一律继续走正常分流**：`问：` 去答、祈使句落教材、派活进候选。
  私聊里的消息**永远**要么被处理、要么被明确回执，不允许"当成答复"然后消失。
- 求助加 **30 分钟 TTL**（`teach.ASK_TTL_MS`）：过期不再参与匹配，问句留在 `questions.jsonl` 可查。
- 求助正文改口径：把"直接回我一句就行"改成「**引用回复我这条**，写 A/B/C 或你自己的话，
  没引用我分不清你在答哪一条，30 分钟后作废」。**不能一边要求引用、一边在文案里鼓励不引用。**

### 24.4 判据（可复跑）

```bash
python3 -m pytest tests/test_service.py -k TeachAnswer -q   # 7 条：引用才落教材；无引用不吞、不猜
python3 -m pytest tests/test_teach.py -k QuestionBookkeeping -q  # 含过期：过期求助不算待答、引用也不认
python3 -m pytest tests/test_consumer_escalation.py -q      # 求助文案必须写明「引用回复我这条」
python3 -c "import pathlib,sys; sys.path.insert(0,'.'); from bridge import teach; \
print(len(teach.pending_questions(str(pathlib.Path.home()/'Library/Application Support/FeishuPersonalAgent'))))"
# 线上还有几条待答（应为 0；或都未过期——过期的不再算待答）
```

真机回归（2026-11-06 09:19）：私聊发一条不带引用的新指令（算术题）→ 事件正常生成、答复「17」、
`reply_verified`；当时待答求助 = 0（两条旧的已过期），不再出现"对不上号"。

### 24.5 一条被误记成"教材"的提问（当晚已撤回，这里只留原因）

`teachings.jsonl` 里的 `t-0002` 是 11-05 下午被误判成"教材"的一句**提问**
（「你的信息库在哪？你能读什么、能不能改东西？」）。判读规则当时就修了（`classify` 现在把这句判成
`question`），脏数据当晚也用 `teach revert t-0002` 撤了：今天 `teach list` 只剩 `t-0001`（信息库与
只读边界）和 `t-0003`（称呼「宝哥」），`state()['charter']` 同样只有这两条。

这次没有再做任何清理动作，只把原因记清楚——教训是**修了机制不等于清了数据**：机制第二天就对了，
脏条目却在教材里多待了一下午，每一轮问答都在拿它当"准则"。

## 25. 判读该不该交给模型：用真机语料量一次（2026-11-06）

### 25.1 起因

本人看完 11-05 的事故后提了一个设计质疑，一句话：

> 「是不是每个消息都要经过 LLM 处理，还是说 bridge 先按规则处理，但是，语言这种灵活性很强的
> 表达方式，怎么可能有明确的规则呢，我建议还是，每条消息都经过 LLM 去分析，这个消息的意图是什么。」

这不是能靠辩论定的事，所以先量。

### 25.2 现状先说清楚：走模型的与走规则的

| 通道 | 谁在判"这句话是什么" |
|---|---|
| 群聊 @ 我（真正干活的入口） | **消费者（模型）**。bridge 只做结构判断：@ 了本人没有、发送者是不是本人、是不是自己发的、这条见过没有 |
| 教材私聊（自己 @ 自己） | **bridge 的规则**：`teach.route_decision` = 精确命令 → 前缀（`问：`/`做：`）→ `classify`（4 组关键词 + 句尾问号）。**这一行到 2026-11-06 晚已过时：见 §25.5，判读整条交给模型了** |

所以"每条消息都过 LLM"对干活的那条路**本来就是这样**；用规则的只有准入（判的不是语言，是
"这条算不算数、归谁"）和教材私聊的意图判读。本人的质疑落在这第二处，是对的。

### 25.3 量出来的数（可复跑）

语料 = 他 11-05 在私聊里真说过的 20 句（`tests/corpus/teach_intents.jsonl`，已脱敏，
`want` 是他当时的真实意图）。规则侧：`python3 tools/teach_eval.py`（**该工具已随 §25.7 删除**：
规则没了，就没有「规则分流正确率」可测；语料 `tests/corpus/teach_intents.jsonl` 仍保留，
用来给 prompt 侧做人工对账）。

| 判读器 | 结果 | 错在哪 |
|---|---|---|
| 规则（`teach.classify`） | **12/20（60%）** | 8 条全错在"明确的派活被判成陈述"：把 17 和 25 相加、这是第 3 步…、再补一件…、自检 1+1… |
| 模型（同一语料，同一标签集，单次调用） | **19~20/20（95~100%）** | 只有「自检…1+1 等于几？只回一行」判成 task、预期 question——两种去向相同，实际无差别 |

**规则错判的代价不是"多一句废话"**：判成陈述就进单槽候选，回执问「回『做』派活 / 回『记』/ 回『不记』」，
他要动手还得先回一个"做"字。他 11-05 下午连发四条分步指令，每条都被这一轮确认拦一下——
「你笨笨的」「你蠢蠢的」大概率就是这么来的。

（模型这一侧怎么跑出来的：把同样的 20 句 + 标签定义 + "只输出 JSON" 给一个模型，提示词与输出
都记在 `tools/teach_eval.py` 的说明里，任何人可复跑；**工具已于 §25.7 删除**，数字以本节为准。）

### 25.4 结论与边界

1. **教材私聊的意图判读应该交给模型**，规则只留"协议词"（`复盘/教材清单/忘掉 t-0007/记/不记/上班/下班`）
   ——那是协议，不是语言，交给模型反而会判错。
2. **准入不许交给模型**：`@ 了自己`是零账本的自激防线，`发送者是不是本人`是身份边界。
   这些不是语言判断，模型判错一次就是循环/越权。
3. 剩余的真问题不是"判得准不准"，而是**判完之后谁动手**：模型判出"这是教材"，要么 bridge 按结果落盘，
   要么模型自己调 `feishu-bridge teach add` 落盘。后者更彻底（bridge 从语义里退场），但要先补
   `teach add` 这个 CLI（现在只有 list/show/revert/enable/disable/tidy/status，没有 add）。
4. 风险与缓冲（切之前必须补齐）：教材私聊会从"秒回"变成"等模型 10~15 秒"；同一句话两次判读
   可能不同 → 同一条消息只落一次（幂等）+ 回执必须报"我读成什么" + `忘掉最后一条` 可撤回；
   模型答不上时**可见降级**（回执说"没判出来，回『记/做/不记』我按字面办"），不许静默丢。

### 25.5 切换（2026-11-06 晚，本人选"只回一条，接受等 10~15 秒"）

问到"切了之后这条私聊的回执怎么给"时，本人选的是**只回一条**（不要"秒回收到 + 随后结果"两条）。
所以整条链只有一个出口：模型答复的那一条。

**改法（删除优先，不留两套）**

| 位置 | 改了什么 |
|---|---|
| `bridge/teach.py` | 删掉 `classify` 与四组关键词表（`_QUESTION_MARKERS`/`_TASK_MARKERS`/`_COMPLAINT_MARKERS`/`_IMPERATIVE_MARKERS`）。`route_decision` 只认三类协议信号：精确命令、`问：`/`做：` 前缀、`原则：` 之类标签；其余一律 `infer`。新增 `add_from_model(home, kind, text, message_id)`：模型侧唯一写盘入口，写盘规矩（类型白名单/长度/条数上限/**按来源消息幂等**）仍在这一层 |
| `bridge/service.py` | `route_self_chat` 的非协议分支从"判读 + 进候选 + 回执"改成一行：`{"text": text, "self_intent": "infer"}` 交给消费者。删掉"派活/评价/陈述进候选"和"提问直连"两条规则分支；走到未覆盖分支时显式 `AssertionError`（静默 `return None` 就是消息不见了） |
| `agent_prompt.py` | 新增 `self_intent == 'infer'` 段：先判它是哪一种（教材/派活/提问/吐槽），教材就给 `teach: {kind, text}`，**不许自己敲命令写教材**；判不准先按"不记"处理并问一句。答复 JSON 契约相应加上 `teach` 对象 |
| `consumer_safety.py` | `finalize_reply` 多返回一个 `teach`，`normalize_teach` 逐项校验（kind 白名单 6 类、正文非空 ≤400 字），坏字段直接丢弃——结构化字段出错不能连累正文 |
| `consumer_runtime.py` | 回复提交成功后调 `feishu-bridge teach add … --message-id <来源消息>`；成功/失败都记进 attempt journal（`taught`），崩溃恢复不重记 |
| `bridge/cli.py` | 新增 `teach add`（打印 JSON，重复调用给 `duplicate: true`）；新增 `_park_failed_teach`：模型判读失败（事件 dead）时把原话放进候选槽，回「记/做/不记」仍然有效 |
| `consumer_runtime.py`（通知） | 模型失败的回执按 `self_intent=infer` 换文案：直接给三个出口（记/做/不记），不再是"引用回复我这条" |

**为什么候选槽没删**：它还有两个活来源——`复盘`给出的候选教训（本人回「记」才落盘）、
以及上面那条"模型失败暂存"。删了就只剩"这句消失了"。这不是两套实现，是同一张槽的两种入口。

**判据（都跑过）**

- `python3 -m pytest -q` → **1020 passed**
- `python3 tools/teach_eval.py`（**已删除**，见 §25.7）→ **分流 20/20 对**：规则自己定 3 条（命令/前缀/标签），交模型 17 条
- 新增回归：模型判成教材要真落盘且按消息幂等（`tests/test_teach.py::ModelTeachingTests`）、
  同一句重放不记两条（`test_consumer_escalation.py::ModelTeachingTests`）、坏 `teach` 字段不挡正文、
  失败事件把原话放进候选（`tests/test_cli.py`）、prompt 必须要求 `teach` 对象且不许让模型自己敲命令
- 删掉/改写的旧测试：`VerdictTests`（8 条，判读函数已不存在）→ 换成 `RouteDecisionTests`（钉边界：
  什么仍由规则定、什么必须交给模型）；`TeachConfirmationTests` 6 条按新契约重写

**真机验收（2026-11-06 晚，release 重装后）**：见 §25.6。

### 25.6 真机验收与一个只有真机才炸的坑

验收（2026-11-06 晚，release 重装后，用 lark-cli 以本人身份在教材私聊里发真消息）：

| 发的 | 期望 | 实际 |
|---|---|---|
| 「测试判读：这条不要记成教材，只回我两个字：收到」 | 判成"派活/非教材"，只回一条，教材不变 | 回了「收到」，教材仍是 2 条 |
| 「以后汇报先给结论，再讲细节」 | 判成教材并写进去 | 回了「记下了：…要撤回说「忘掉最后一条」」，但**教材还是 2 条** |

第二条暴露一个只有真机才炸的坑：**`teach add` 被拼成了 `feishu-bridge events teach add …`**。

- 原因：`consumer_runtime.command` 里只有 `notify` 走顶层命令，其余一律加 `events` 前缀。
  我新加的 `teach add` 也是顶层命令，于是被拼成事件子命令 → 非零退出 → 被 `except` 吞掉
  → journal 里 `taught: {ok: false}`，而回复正文已经跟本人说了"记下了"。**测试全绿也照样炸**，
  因为 FakeBridge 直接按 `argv[0]` 分发，从不检查前缀。
- 修法：`TOP_LEVEL_COMMANDS = ('notify', 'teach')`；并且**写盘失败必须可见**——
  `notice_teaching_failed` 发一条「【教材没记上】…回「记」我按原则重记一次」，
  不能出现"回了记下了、什么都没写"。
- 新增判据：`tests/test_consumer_runtime.py::TopLevelCommandTests`（钉前缀）、
  `tests/test_consumer_escalation.py::test_a_failed_write_is_reported_instead_of_letting_the_reply_lie`。

**教训**：`argv[0]` 分发的假 bridge 测不出"命令拼错"，它测的是"我调用自己写的那套约定"。
凡是跨进程的命令构造，判据里必须有一条直接断言**最终 argv**。

> **后续**：§25.7 又往前走了一步——命令词、`问：`/`做：` 前缀、类型标签、以及本节依赖的单槽候选
> 全部删除。本节描述的是当时的中间状态，别照着它改代码。

### 25.7 第二阶段：连协议信号也删掉（2026-11-06 深夜，本人第三次拍板）

§25.5 切换之后，教材私聊里还剩三种"协议信号"：

1. **精确命令词**：`教材清单`、`忘掉最后一条`、`忘掉 t-0007`、`记`、`不记`、`答`、`做`；
2. **路由前缀**：`问：` / `做：`（还有 `提问：` / `干活：` / `安排：`）；
3. **类型标签**：`原则/风格/反馈/事实/资料/指针/教训：`。

本人看完 §25.5 的说明后给了一句结论：

> 「让用模型去判断是什么指令，而不是协议信号和规则！」

这句不是否定 §25.2 的实测（规则 12/20、模型 19~20/20），而是把同一个理由推到底：既然"这句话是
什么意思"已经交给模型，那么"这句话是不是一条命令"也是同一个问题，留下一半就是两套判读并行
（AGENTS.md 第一原则：两套实现并行 = 未完成的重构）。而且信号本身有代价——`原则：` 记错类型、
`记`/`做` 记错对象，都等于替本人改了教材或派了活。

**改了什么（按文件）**

| 文件 | 改动 |
|---|---|
| `bridge/teach.py` | 删 `route_decision`、`command_for`、`intent_for`、`is_tagged`、`parse_text`、`TAG_PREFIXES`、`_FORGET_ID`、`_ASK_PREFIXES`、`_TASK_PREFIXES`；删单槽候选整套（`pending_path/set_pending/read_pending/clear_pending/render_pending_ack`、`PENDING_TTL_MS`）；删只被这些用到的回执渲染（`render_ack/render_forget/render_list/_describe/_number/COMMAND_LINE`）。留下的是"不是语言判断"的两件事：**模型判出教材后的写盘**（`add_from_model`，白名单/长度/条数/按消息幂等）与**求助的待答登记** |
| `bridge/service.py` | `route_self_chat` 只剩四步：准入 → 回放去重 → 非文本退回 → 引用答复；其余一切 `self_intent=infer`（正文只去 @ 占位符）。删 `_run_self_command/_reflect/_pending_as_event/_confirm_pending/_find_teaching` 与 `turns` 导入 |
| `bridge/cli.py` | `teach revert <id>` 加 `--last`（模型要能执行「忘掉最后一条」）；新增只读 `teach reflect`（打印 `turns.reflection` 的同一份报告）；`teach status` 去掉 `pending_teaching`；删 `_park_failed_teach` 与 `_events_fail` 里的调用 |
| `agent_prompt.py` | `self_intent=infer` 那段重写：**你是唯一判读者**，连"看起来像命令"的话也一样；教材仍走 JSON `teach` 字段（不许自己敲命令写），而清单/看一条/撤回/复盘明确给 CLI（`teach list|show|revert| --last|reflect`）；删掉已无入口的 `ask`/`task` 前缀分支 |
| `consumer_runtime.py` | 失败提醒与"教材没记上"提醒不再教人回「记/做/不记」，改成"把这句话再发一遍" |
| 测试 | 删 `RouteDecisionTests/PendingSlotTests/IntentRoutingTests`、`TeachConfirmationTests/TeachIntentTests/ReflectionTests`（service 侧）；新增 `NoRoutingLeftTests`（源码里不许再有判读函数，钉字符串名单）与 `TeachIntakeTests` 里"任何文本都变成 infer"的逐条用例（含 `教材清单/复盘/忘掉最后一条/记/答/做/原则：/问：`） |
| 文档 | `CONTRACTS.md` 的「教材通道/分流/归因与复盘/文件/CLI」全部改成"没有协议信号"；`skills/feishu-bridge-consumer/SKILL.md` 的 `self_intent` 说明改为 `infer` 单一值 |

**代价（明说，不藏）**

- **私聊派活不再自动开任务账本**：账本原来由 `self_intent=='task'`（`做：` 前缀）触发；信号删了
  就没有"这是派活"的确定性输入，bridge 猜不出该不该建账，所以不开——宁可不开，也不开一条错的账。
  要多轮账本时仍可用 `events reply --task-id` 指向已有任务。要恢复这一层，正确做法是给模型一个
  显式的建账出口（答复 JSON 里加 `task.create`），不是把前缀加回来。
- **判读失败没有候选兜底**：单槽候选随 `记/不记` 一起删了。模型超时/出错时只剩一条可见提醒
  （"把这句话再发一遍"），原话留在死信事件的 payload 里可查。
- **答复不再顺手记成教材**：以前引用答复会把答复文本落一条教材；现在答复只进 `questions.jsonl`
  并重放原事件，要记成教材得本人明说、模型判（少一次"误记"的来源）。
- 私聊每条消息都多一次"可能跑 CLI"的机会：查清单/复盘比原来慢（模型要跑命令再答），
  实测仍在单条回复的时延预算内（collect+consumer+deliver 口径不变）。

**真机验收（2026-11-06 10:24~10:41，私聊里由本人账号发的四句）**

| 发的 | 期望 | 实际 |
|---|---|---|
| 「教材清单」 | 模型自己跑 `teach list` 并把条目报回来 | 回了 2 条条目 + "以上是 `feishu-bridge teach list` 的原样输出" |
| 「以后给我汇报先给结论，再讲细节」 | 判成教材并落盘 | 回了"记下了：那句话；要撤回说「忘掉最后一条」"，`teach list` 多出 `t-0005 · 风格`，正文就是他说的话 |
| 「忘掉最后一条」 | 撤掉 `t-0005` | 第一次**失败**：模型自己跑 `teach revert` 被沙箱拒（见下）；修完再发 → 版本 7→8，教材回到 2 条，单条回复 |
| 「复盘一下」 | 模型跑 `teach reflect` 转述 | 回了 `复盘（最近 16 轮）· 答完 16 次 / 失败 0 / 超时 0 · 教材平均每轮 2.1 条` 的原样输出，并明确"这次不编教训" |

**只有真机才会暴露的第二个坑：模型自己跑不动写盘命令。** 消费者是 DSH headless，沙箱是
`workspace-write`、审批默认关闭；`~/Library/Application Support/FeishuPersonalAgent/` 在工作区外，
于是 `teach revert` 创建 `teach.lock` 时 `PermissionError`，模型只能在私聊里发一条
「【需要你出手】…撤回要写 teach.lock，被沙箱拒绝…A/B 你选」。**读命令（list/reflect）没问题**，
写命令一定撞墙。这其实印证了 §25.5 定下的规矩——写盘只有 bridge 一份实现、模型只报意图——
只是当时只把「记」做成了 JSON 字段，忘了「撤」。修法：答复 JSON 加 `teach_revert`（id 或 `"last"`），
运行时在回复发出后调 `teach revert`，失败补一条「【教材没撤掉】」提醒（去重键 `revert:<事件id>`）。
（**第二天就被同一节 §25.8 改掉了**：提醒并入回复末尾的回执，不再单发第二条消息。）

**判据（可复跑）**

```bash
python3 -m pytest -q                                   # 997 passed（当时；现在见 §26）
python3 -m pytest tests/test_repo_hygiene.py -q        # 3 passed
python3 -m pytest tests/test_consumer_escalation.py -q # 15 passed：顺序/回执/失败/先撤后记/不二次求助
python3 -m pytest tests/test_teach.py -q -k NoIncantation   # 旧口令在面向前台的源码里零残留
grep -rn "command_for\|intent_for\|is_tagged\|parse_text\|route_decision\|pending_teaching\|_park_failed_teach" \
  bridge/ consumer_*.py agent_prompt.py dsh-consumer.py   # 只允许出现在"已删除"说明与测试里
```

**真机验收（2026-11-06 11:5x，release `e0d293a84d9c7a53`，本人账号在私聊里发）**

| 发的 | 实际回复的关键行 |
|---|---|
| 「以后你给我汇报，先给结论，再讲细节」 | 正文（理解 + 用途）+ `· 记在「风格」t-0006 —— 觉得不对，照平时那样 @ 我，说「撤回 t-0006」`；`teach list` 版本 8→9 |
| 「撤回 t-0006」 | 正文 + `· 撤了 t-0006（风格：以后你给我汇报，先给结论，再讲细节）—— 以后不按它办。`；版本 9→10，教材回到 2 条 |

两处仍不完美（记着，不当成"已解决"）：① 署名（`此回复信息来自…`）由 bridge 在发送时追加，
所以回执不是最后一行（§25.9 之后已把署名排到回执上面）；② 模型把撤回说成"删除记录"（实际是作废、
历史保留），已在 prompt 里补了"作废 = 以后不再生效，不是删除历史，回复里也别说删除"。

**又过了半小时，本人把回复本身的标准定了下来（当天第三次纠偏，直接推翻上面的形态）**：
回执不是"要撤回说「忘掉最后一条」"这种口令，而是四要素——**记在哪（档位 + 条目号）、
记了什么、以后有什么用、觉得不对怎么撤（口令带条目号）**。要满足这条，顺序必须反过来：
先写盘拿到 `t-0005`，再发回复；回执由运行时追加，模型正文**不许**说"记下了"（写失败时那句话
已经发出去了，等于撒谎）。同一轮还清掉两处残留：一是"忘掉最后一条"这种本人才需要背的暗号
（协议时代的口令，删了协议却留着提示语），二是教材私聊里多余的 `ask_owner`（这条回复本身就是
私聊发给本人的）。详见 §25.8。

---

### 25.8 回执四要素：先写盘、后回复，成败只由一行说（2026-11-06 中午，本人第四次拍板）

问题不是"记没记住"，是**本人看不懂我们在说什么**。原样三连问（"可撤销是什么意思""没人知道这个
可撤销是什么意思""忘掉最后一条是什么意思"）指向同一个缺陷：**系统对着本人说内部黑话**。
§25.7 之后的回复是"记下了：那句话；要撤回说「忘掉最后一条」"——既没有条目号（撤不了"这一条"），
也没有"以后用在哪"，而且要求本人背一句固定口令。

**改法（一句话）：写入先于回复，回执由运行时代写，模型只说自己理解成什么、以后怎么用。**

```
模型 → {"answer":"我理解成：以后给你汇报，先给结论，再讲细节；你要细节会说「展开」。",
        "teach": {"kind":"style","name":"汇报先给结论",
                  "text":"以后汇报先给结论，再讲细节"}}
运行时 → feishu-bridge teach add … → 拿到 t-0005
       → 发：answer + "\n\n· 记在「风格」t-0005 —— 觉得不对，照平时那样 @ 我，说「撤回 t-0005」"
```

| 文件 | 改了什么 |
|---|---|
| `consumer_runtime.py` | 动作（`teach add` / `teach revert`）从"回复发出之后"移到**之前**；新增 `action_receipts()` 生成回执行（`· 记在「风格」t-0005 —— …说「撤回 t-0005」` / `· 没记上：原因 —— 这句没生效` / `· 撤了 t-0005（风格：…）—— 以后不按它办` / `· 没撤掉：…`）；同一条回复里先撤后记；`self_intent=infer` 的事件丢弃 `ask_owner`（私聊里再私聊=重复噪音）；删掉 `notice_teaching_failed`/`notice_revert_failed` 两个"补发第二条提醒"的函数（回执已承担） |
| `consumer_safety.py` | `finalize_reply` 多返回 `dropped`（字段写了但被丢弃的名字）——**丢弃不静默**，运行时据此写"没记上：类型或正文不合规" |
| `bridge/cli.py` | `teach add` 失败不再非零退出（改为 `{ok:false,reason}` 退出 0，运行时才能把原因写进回执）；`teach revert` 改成 JSON（`{ok,id,kind,label,text}`），`--last` 解析出的条目号一并返回；`enable`/`disable` 提示语去掉「忘掉最后一条」 |
| `bridge/teach.py` | 条数满/停用提示从「忘掉 t-xxxx」改成「撤回 t-0005」 |
| `agent_prompt.py` | 判读段重写：① 教材只报 `teach`，正文说"理解成什么/以后怎么用"，**禁止**说"记下了"、**禁止**让本人背固定说法；② 撤回写 `teach_revert`（不知道号写 `last`）；⑤ 只表达态度时给一个**封闭问题**（两个选项），第二次还含糊就给默认并让本人喊停，抱怨里带具体做法时按 feedback 记；私聊里禁用 `ask_owner` |
| `CONTRACTS.md` | 教材通道表补"回执四要素"与顺序、失败语义、先撤后记、`dropped`、私聊不用 `ask_owner` |

**代价与边界**：回复会晚 ~0.2 秒（先写盘）；写盘成功但发消息失败时，重试会重发（`add` 按来源
消息 id 幂等，回执里的条目号不变）；`dropped` 让契约多一个字段（`{'answer','ask_owner','task','teach',
'teach_revert','dropped'}`）。

### 25.9 名称取代编号（2026-11-06 中午，本人第五次拍板，一句话）

> 「编号读不懂，有一个名称看起来直观一些。」

上一节把回执写清楚了，但句子里带着 `t-0006` 这种编号——它是我这边账本的钥匙，不是人看的字。
于是每条教材加一个 **2~16 字的名称**，回执与撤回都用名称：

```
我理解成：以后给你汇报，先给结论。（模型正文）
· 记在「风格」· 汇报先给结论 ｜ 觉得不对，照平时那样 @ 我，说「撤回 汇报先给结论」
```

| 文件 | 改了什么 |
|---|---|
| `bridge/teach.py` | 条目加 `name`（`normalize_name` 清洗、`auto_name` 从正文取一段兜底、同名加 `（2）` 不拒绝写入）；`_views` 给旧条目按正文补名字（历史不可变，不改写日志）；`find_entry`/`live_entry_id` 接受编号或名称 |
| `bridge/cli.py` | `teach add --name`；`teach list` 名称在前、编号在后（编号留给模型撤回时用）；`teach revert` 编号与名称都收 |
| `consumer_safety.py` | `teach` 对象多接受 `name`（写坏只丢名字，不能让整条记不上）；`MAX_TEACH_NAME_CHARS=16` 与 bridge 侧同一份事实 |
| `consumer_runtime.py` | 回执用名称：`· 记在「风格」· 汇报先给结论 ｜ …说「撤回 汇报先给结论」`、`· 撤了「汇报先给结论」（风格：…）—— 以后不按它办` |
| `agent_prompt.py` | 教模型给 `name`（概括这条管什么）、撤回前先跑 `teach list` 找编号、报给本人时只说名称不说编号 |

**真机验收（release `655c536f6e9b6545`，2026-11-06 12:1x）**

| 发的 | 回执关键行 | 状态变化 |
|---|---|---|
| 「以后给我汇报，先说结论，再讲细节和依据」 | `· 记在「风格」· 汇报先给结论 ｜ …说「撤回 汇报先给结论」` | 版本 10→11，新增 `t-0007 · 汇报先给结论` |
| 「撤回 汇报先给结论」 | `· 撤了「汇报先给结论」（风格：以后给我汇报，先说结论，再讲细节和依据）—— 以后不按它办。` | 版本 11→12，回到 2 条 |

**已知瑕疵**：① 旧条目（`t-0001`/`t-0003`）没有名称，读时从正文截取，`t-0001` 截成了
「你可以用本机的 Docu…」——要好看得有个"改名"动作（`rename` 记录 + 重放），目前没做，
本人在私聊里重说一遍就会得到一条带好名称的新条目；② 署名（`此回复信息来自…`）由 bridge 发送时
追加，仍在回执下面。

**判据（可复跑）**

```bash
python3 -m pytest -q                                   # 1004 passed（当时；现在见 §26）
python3 -m pytest tests/test_teach.py -q -k EntryName  # 名称：存储/兜底/旧条目/同名/按名称查
python3 -m pytest tests/test_cli.py -q -k name         # 名称可替代编号
feishu-bridge teach list                               # 名称在前、编号在后
```

---

### 26. 三件事一起做：改名、署名位置、到点提醒（2026-11-06 下午，本人「1 2 3」）

§25.9 结束时留了三件事，本人回了一个「1 2 3」表示全做。

**① `teach rename`：只改名字，不动内容。** 旧条目是自动取的名字，`t-0001` 被截成
「你可以用本机的 Docu…」。日志是只追加的，所以改名写一条 `rename` 记录（`{op:'rename',id,name}`），
重放时覆盖名称、正文与其它字段一个字不碰；`teach rename <编号|名称> <新名称>`，同名自动加序号。
顺手把两条旧条目命名成「信息库与只读权限」「称呼宝哥」。

**② 署名挪到回执上面。** 原因很朴素：落款夹在附注中间读起来像没写完。改法不是"让模型先别写回执"，
而是把三段拆开、各归各家：正文归模型（stdin）、回执归运行时（`--receipt-file`）、
落款归 bridge（`render_reply(answer, receipt=…)` → 正文 → 署名 → 空行 → 回执）。
回执也走文件而不是 argv：正文因为防 `ps` 泄露只从 stdin/文件读，回执里有教材名称，同一个道理。
超长时只截正文——落款被截掉等于没落款，回执被截掉等于做了不说。

**③ 到点提醒（`remind`）。** 为什么在 bridge 而不在消费者里"睡一觉再发"：消费者会话随时超时/重启/
换 release，睡在它进程里的定时器一定丢；bridge 是这台机器上唯一长期活着的进程，而且已经握着发送通道。
存储是 `state.sqlite3` 的 `reminders` 表（schema v7）：`due → claim(pending→sending) → send_text → sent`，
发失败退 pending、连续 3 次记 failed；心跳（默认 3s）里跑，挂起期间不响、恢复后补发并注明「晚了 N 分钟」。
正文由 `bridge/remind.body_for` 统一拼：**绝不 @ 人**（@ 会给 bridge 自己造出下一条待办），并带署名。
时间只认本地时区，`2026-02-30` 这种"没有这一天"直接报错而不是替本人滚到 3 月。

**这一轮踩的四个坑，每个都对应一条已加上的门禁**

| 坑 | 表现 | 现在怎么挡 |
|---|---|---|
| `remind` 不在 `TOP_LEVEL_COMMANDS` | 被拼成 `feishu-bridge events remind add`，非零退出被 try/except 吞掉 → 回复写"提醒没设上：提醒命令失败" | 白名单补 `remind`，`tests/test_consumer_runtime.py` 钉住这张表；同一个坑今天踩了两次（teach add、remind add） |
| CLI 写 `bridge.db`、服务读 `state.sqlite3` | 回执说"设好了"，到点一声没响，`remind list` 还显示得好好的 | 统一到 `state.sqlite3`；`tests/test_cli.py::RemindCommandTests` 断言不许出现 `bridge.db` |
| `create_at` 没进模型能看到的 JSON | 模型算不出绝对时间，只能含糊其辞 | `build_prompt` 把 `create_at` 放进 `data`；`tests/test_agent_prompt.py` 钉住 |
| 提醒正文本人看起来像自己发的 | 没署名 | 走 `render_reply` 加署名；`tests/test_service.py::ReminderTests` 钉住 |

**真机验收（release `69f6aca12c272a0e`，2026-11-06 14:3x–14:5x，本人账号私聊）**

| 发的 | 结果 |
|---|---|
| 「以后跟同事说话，别用敬语，直接说事」 | 回复里署名在**上**、回执在下：`此回复信息来自…` + `· 记在「风格」· 对同事不用敬语 ｜ …说「撤回 对同事不用敬语」`（测试条目随后已撤回，教材回到 2 条） |
| 「3 分钟后提醒我给张三打电话」 | 回执 `· 提醒设好了：2026-11-06 14:42（1 分钟后）｜ 不想提醒了，@ 我说「取消 打电话给张三」`；14:42 收到 `⏰ 提醒你：给张三打电话 / （你 2026-11-06 14:40 让我这个点提醒你）`，待发列表归零 |
| 「别提醒了，把喝水那条取消」 | 回执 `· 提醒撤了：「喝水」（原定 2026-11-06 14:54）—— 到点不响了。`，`remind list` 变 0 条 |
| 「1 分钟后提醒我站起来活动一下」 | 14:48 收到提醒，**带署名**、不带 @ |

**判据（可复跑）**

```bash
python3 -m pytest -q                                     # 1049 passed
python3 -m pytest tests/test_repo_hygiene.py -q          # 3 passed
python3 -m pytest tests/test_remind.py tests/test_service.py -q -k "Remind or Body or Parsing"
python3 -m pytest tests/test_cli.py -q -k Remind         # 提醒落在服务读的同一个库
feishu-bridge remind list                                # 待发提醒（0 条即全部处理完）
feishu-bridge teach list                                 # 名称在前、编号在后
```

**还没做的**：重复提醒（「每天早上提醒我」明确说做不到）、提醒的 @ 对象（现在只发到会话，不 @ 人）、
提醒的历史列表（只列还没发的）。

**同一轮收代码审查，改掉的四处**（review 之后又跑了一次全量，数字见上）：

| 审出来的 | 怎么改的 |
|---|---|
| `reminders` 的建表 SQL 在 schema 脚本和 v7 迁移里各写一遍 | 只留 schema 脚本那一份（`CREATE IF NOT EXISTS` 老库也会补上），迁移分支只推进版本号——与 v5 的 tasks 同一套做法 |
| `REMINDER_LIVE` 定义了没人用，四个查询各自硬编码 `('pending','sending')` | 从常量生成 SQL 占位符，四个查询共用 |
| `teach_revert` 只收 `t-0007`/`last`，但回执教本人说的是「撤回 汇报先给结论」 | 名称也收（不含空白/引号/路径样式/`-` 开头，≤16 字），CLI 本来就按名称查得到 |
| `remind status` 的「最近失败」按到点时刻排序 | 改按失败发生的时刻（`fired_at_ms`），并加一条能区分两种排序的测试 |
| 提醒卡在 `sending` 永不回收（静默丢一条） | 服务启动时 `recover_reminders()` 放回 pending |
| 消费者侧复制的上限/类型常量声称"测试钉住"但没测试 | 真加了钉住测试；正文上限则钉"不许比 bridge 更松" |

---

## 27. 同事 Mac 升到最新 2.0.x：v6→v7 就地迁移 + 消费者换 release（2026-11-06 15:2x）

**背景**：同一台同事 Mac（前排 §23 那台，tailnet 内 `<同事机>`）从 release `de65090fb8580c03`
（2.0 首版，schema v6）升到代码 HEAD `29e601c`（teach 改名、`remind` 一次性到点提醒、回执位置调整、schema v7）。
这台机器昨天之后已经**有真实流量**：升级前 events/inbox/outbox = **9/16/8**、consumers=1。

**跨机器一致性判据（这次新加的，比 git sha 更直接）**：release id 是 `bridge/*.py` 的哈希，
所以先在管理员机上对 HEAD 现算一遍，再和对方机器上 `installed/running` 比：

```bash
python3 -c "from bridge import release; print(release.release_id('.'))"   # → e4fa5e4c28054914
```

升级后她机器上 `doctor --assert` 的 `release_match=ok | installed=e4fa5e4c28054914 running=e4fa5e4c28054914`
——同一份代码在任何机器上算出的 id 相同，"跑的是不是那份代码"不依赖 git 状态。

**步骤**（与 §23 完全相同，macOS 没有发行包这一层）：

```bash
sqlite3 "$H/state.sqlite3" ".backup '$H/pre-v7-state-$(date +%Y%m%d-%H%M%S).sqlite3'"
tar xzf feishu-v7.tgz -C /tmp/src-v7 && rsync -a --delete /tmp/src-v7/ ~/feishu-personal-agent/
python3 install.py --with-agentkit --agentkit-agent dsh --agentkit-consumer agentkit-dsh --verify
python3 "$H/current/agentkit-consumerctl.py" restart
```

**真机数字（15:24–15:27）**：

- 安装：源树/目标树都是 **189 个文件**（镜像，不是覆盖），`install rc=0`、receipt `verdict=ok`。
- 库：`user_version 6 → 7`，新增 `reminders` 表（`reminders=0`）；events/inbox/outbox/consumers 仍是
  **9/16/8/1**，状态分布没变。备份 `pre-v7-state-20261106-152441.sqlite3`（225 KB，integrity ok，v6）。
- 消费者：`ps -o args=` 从 `releases/de65090fb8580c03/...` 换成 `releases/e4fa5e4c28054914/...`；
  `consumer status` 里 `agentkit-dsh live=true`。`fails=5` 仍是升级前的历史计数，没有新增。
- 配置：`_merge_config_defaults` 补上了 2.0 新键（`self_scan_seconds=10`、
  `search_min_interval_seconds=1.0`），既有值一个没动；`config.release_id == installation.release_id == running_release`。
- 新能力可用：`feishu-bridge --help` 多出 `remind`，`remind list` → 「待发提醒 0 条」；
  `latency` 8 个样本 `answered`、`pending=0 deliver_missing=0 negatives=0`。
- 真机 @ 闭环：群里发 `om_x100b61ec4166e4b4df6833af59a07cc`（15:25），她回
  `om_x100b61ec5cdd506cc0450c2f6931436`（15:26，`sender_type=user`、`reply_to` 正确），
  正文 + 「此回复信息来自<本人>的 AI 助手」；event 11 `done`、outbox `reply_verified`。
- 稳定性：升级后 `doctor --assert` 连跑两次 `rc=0`；pid 不变、心跳 1–3 秒；日志 15:2x 窗口内无 error/traceback。

**回滚边界**：旧 release `de65090fb8580c03` 还在 `releases/`，但库已到 v7（v7 迁移只能前进）。
回滚 = 停 bridge 与消费者 → 恢复 `pre-v7-state-20261106-152441.sqlite3` → 装旧包 → 起。

**收尾干净化（同一件事的另一半：升级不能在机器上留中间物，2026-11-06 15:4x）**：

- 部署临时物全清：对端 `/tmp` 里的源码包、解包目录、`install-*.out/err`、`doctor-*.json` 归零；
  管理员机 `/tmp` 里历次部署留的 `feishu-*.tgz`、`src-*`、`head-check/head-clone`（纯净克隆验 HEAD 用）
  一并删掉。这些都不是"下次还能用"的资产——源码包可以从 `git archive` 重出，纯净克隆是一条命令的事。
- 库快照从 home 根目录挪进 `<home>/backups/`，并改名成 `state-v<旧版本>-pre-<动作>-<ts>.sqlite3`
  （`state-v3-pre-bridge-2.0-…`、`state-v6-pre-schema-v7-…`）。
- **真机上踩到的坑**：用 `sqlite3 .backup` 拍的副本**仍是 WAL 模式**，之后每读一次就原地长出
  `-wal`/`-shm` 两个旁文件（我先用 `.backup` 拍、又用 sqlite3 查 integrity，于是 home 根和 backups/
  里都堆了碎片）。`VACUUM INTO` 出来的副本是 `journal_mode=delete`、自包含，读多少次都不长旁文件；
  已经用 `.backup` 拍的副本可以 `PRAGMA journal_mode=DELETE` 转一次再归档。这条已写进
  [docs/ops-remote-manage.md](ops-remote-manage.md) §3 的快照配方（原配方只写了 WSL 路径，现在两端共用）。
- 结果：对端 home 根只剩服务的活文件（`state.sqlite3` + 它的 `-wal/-shm`、日志、锁、`releases/`、
  `backups/`），`releases/` 里保留最近几版（产品自己的保留策略，不是中间物）。

**仍未修的误导**（§23 已记一次）：`install.py` 对"已存在的 agentkit 消费者"依旧打印
「本次安装没有装/重载任何事件消费者……下一步 `--with-dsh-consumer`」。这台机器 `consumer_offline=ok`、
消费者一直在 claim，这句 warning 想说的是"我这次没帮你重载消费者"，读起来却像"你现在没有大脑"。
修法（未做）：检测到已加载的 agentkit plist 时，改成「已存在的 agentkit 消费者需要
`agentkit-consumerctl.py restart` 才会换到新 release」并直接打印该命令。

**同一发版窗口里的第二次跟版（15:5x）**：刚装完 `e4fa5e4c28054914`，上游又落了
`38317f9`（`bridge/consumerctl.py`：挂起时别再报「没有活跃消费者」）。这次是**纯代码升级**：
schema 仍是 v7（没有迁移），所以不需要新快照——回滚就是
`feishu-bridge rollback --to e4fa5e4c28054914` + restart + `agentkit-consumerctl.py restart`，
两个 release 都能打开同一个 v7 库。真机判据：`release_match=ok | installed=running=8dcc4cddcfa6ced9`、
`doctor rc=0`、消费者 `ps` 指向 `releases/8dcc4cddcfa6ced9/agentkit-consumer.py`、库仍 v7、
events/inbox/outbox 行数只增不减（11/18/10）。

教训：**部署结论要报 release id，不要报"最新版"**。发版窗口里 HEAD 一小时内走了两次
（`29e601c` → `38317f9`），"最新版"这句话在半小时后就可能指另一份代码；release id 是
`bridge/*.py` 的哈希，两边现算一致才算部署对上了（本轮实测：管理员机 `release.release_id('.')`
与她机器 `installed/running` 三次都相等）。

## 28. 同事 Windows/WSL2 宿主升到最新 2.0.x：镜像打包 + v6→v7 + 全量真机测试（2026-11-06 15:2x）

前排 §23/§27 是同事的 Mac；这一台是**发行包安装的 Windows 宿主**（owner 带人名拼音的旧单元早已迁成
`io.feishu-agent.*`，见 §9.0d），要升到 HEAD `29e601c`（`teach` 改名、`remind` 到点提醒、回执位置、schema v7）。
机器处于**有意挂起**（`mode=held`，`hold.json` 由 `multi-turn-test` 于前一日 19:5x 写下），升级全程不该动它。

**发版**（`dist/wsl2/pack-windows.sh`，输出 `dist/windows/feishu-bridge-windows-20261106.zip`）：

- 172 个文件、1.0 MB、zip sha256 `c3e8dc46…`；`repo_head=29e601c`、`worktree=clean`；
- 包内 `repo/install.py` 与工作树逐字节相同（构建与发版是同一次快照，不是"打包脚本又抄了一遍"）；
- 非 ASCII 文件名（`dist/wsl2/offline/*.cmd`）**全部置了 UTF-8 标志位**——§9.0d 那次远程升级连跑三次
  没装上的根因，现在由 `tests/test_pack_windows.py` 钉住（这一步在包里 172 个文件上逐个断言过）。

**升级**（`tools/bridge-remote.sh upgrade`，rc=0）：

- 落盘：`release_id=e4fa5e4c28054914`，与同日 §27 那台 Mac 算出的 id **相同**——"跑的是不是那份代码"
  不依赖 git 状态，两台机器给同一个答案；
- D 阶段：`MIGRATE clean_count=0`（旧名单元已清干净）；消费者
  `ActiveEnterTimestamp 10:17:10 → 15:24:58`（自证重载）；`FINALIZE_INSTALL=ok`；
- F 阶段逐项：`release_match=ok`、`mode=held`、`hold=dropped=0`、`legacy_units=0`、`consumer_release=ok`。

**库（`user_version 6 → 7`，纯加表）**：新增 `reminders` 表（0 行）；升级前后
inbox/outbox/events/groups/consumers/tasks/task_messages = **127/59/109/141/3/2/4**，状态分布
（completed 61 / dropped 35 / ignored 30 / reply_queued 1）**完全没变**；`hold.json` 一个字节没动
（`active=true`、`since_ms=1793879783752`、`by=multi-turn-test`）。

**判据**：`version` → `installed==running==e4fa5e4c28054914`、`update_pending=false`、`mode=held`；
`check` rc=0（`RESULT=ok`，`consumer_release=ok`）；`ps-tests` 在真机全过
（`WindowsDistribution.Tests.ps1` 190 项 + `InstallWindows.Tests.ps1` 110 项）。

**这次顺序错了，记下来**：我先升级、后拍库快照（`backups/state-v7-post2.2-20261106.sqlite3`，
integrity ok、sha256 `90a30d84…`）——**快照必须拍在升级前**，否则它对新版本是基线、对回退没用。
这次没出事只因为 v6→v7 是纯加表。顺手把这条实测出来的回退捷径写进了
[docs/ops-remote-manage.md](ops-remote-manage.md) §4：把这份快照摘掉 `reminders`/`reminders_due`、
`PRAGMA user_version=6` 之后，用 pre-v7 代码（`SCHEMA_VERSION=6`）打开，
行数与状态分布不变、`integrity_check=ok`——已验证，不是推测。

**没做的事**：机器仍保持 `mode=held`（升级时没有替她恢复是对的）；没有给她发任何飞书消息。
⚠️ 归因更正（2026-11-09）：当时写的"那是她有意挂起的"**是错的**。她 11-05 19:42 已经发过「上班」，
19:56 那次 hold 是同一轮多轮任务测试的收尾误写，机器因此静默 96 小时——完整取证与修复见
[踩坑 23]、以及 `tools/state_guard.py` 的来历。

**同日下午补记（15:37–15:42，两次追加部署）**：验收时顺手看 `consumer status`，发现它在这台**挂起**的机器上
报「当前没有活跃消费者；最近的 dsh-main 是 3xx 秒前（可能已停止）」——消费者进程好着、单元 active，
只是挂起期间 `runtime_gate` 让它不领事件、也不再每轮 `register`，`last_seen_at` 自然变旧。
这是把"按语义置之不问"误报成"消费者挂了"，与删 `consumer-status.json` 那次（把旧引擎停了读成没人回复）同类。

改法只读事实、不改判定：`consumerctl.status()` 用 `bridge.hold.read_state` 带出
`hold{active,since,by,reason}` 与 `idle_expected`，note 在挂起时一律以挂起开头（未过期说"进程在跑但不领事件"，
过期说"预期空闲、release 后一个周期内重新登记"）；`healthy` 仍是"现在有人在领"，不为了让报告好看编 true。
修复过程中又栽了同类一次：挂起刚重启的两分钟里消费者 `register` 过一次，`healthy=true` 而 note 写成
「当前在领事件的是 dsh-main」——第二次改成"只要 `hold.active`，note 就以挂起开头"。三次提交：
`38317f9`（hold 事实 + 判定 + 文档）、`f5cf3d8`（刚登记过 ≠ 在领事件），随后重新打包装机。

真机证据（15:41 那次部署后，release `40dff6dc2223ad29`，与本机 Mac 同一 id）：
- 窗口内（登记 95.2s）：`healthy=true`、`hold.active=true`、note「服务处于挂起…消费者进程在跑…不领事件」；
- 窗口外（登记 253.5s）：`healthy=false`、`idle_expected=true`、note「…这是预期；release 之后一个轮询周期内会重新登记」；
- `check` rc=0（`RESULT=ok`）、`doctor --assert` verdict ok（唯一 warn 是历史 dead 事件）、
  `version` → `installed==running==40dff6dc2223ad29`、`mode=held` 原样。
顺带按修好的 §3 流程补拍了**升级前**快照（`state-v7-pre-upgrade-20261106-1537.sqlite3`，integrity ok、sha `e9476f16…`）。


---

## 29. 「私聊为什么还要登记」+ 登记之后那个必炸的窗口（2026-11-06 傍晚，本人追问 §28）

**本人在另一台机器（同事 Mac）上问的原话**：「前提是这个私聊先被登记 —— 这个怎么登记啊，
这个没法登记，默认就是要开通的啊！」追问的是真问题：为什么一条通道还要"开通"。

**先回答"登记是什么"（§30 之后这步已经不需要人做了，见下）。**
登记 = 一条命令告诉 bridge「哪一个私聊是通道」，落盘成一个文件：

```bash
feishu-bridge teach enable --self            # 自动认出"自己跟自己"那个会话（2026-11-06 新增）
feishu-bridge teach enable --chat oc_…       # 手抄会话 id 的老给法，仍然可用
```

**§30 起：没有这个文件就算默认开通，服务自己认出来写进去，用户一条命令都不用敲。**
它写 `~/Library/Application Support/FeishuPersonalAgent/teach.json`（`enabled/chat_id/enabled_at_ms`），
然后服务才注册 `self` 扫描，用 `{"chat_ids":[那个会话],"is_at_me":true}` 精确搜这一个会话。
**为什么要显式登记**：搜索接口默认只搜群（`chat_type=group`），私聊必须点名才搜——这不是漏做，
是隐私边界：不点名的话等于把所有一对一私聊都卷进采集。

`--self` 的存在就是把这道门槛上的摩擦去掉：那个会话是可算出来的（p2p 且 `p2p_target_id`
等于本人 open_id），不该让人去飞书里翻 `oc_…`。本机实测它认出的正是
教学私聊本身（`teach.json` 里登记的那个 `oc_…`）；认不出或有多个时**报错不猜**（配错会话 = 把别人的私聊当教材）。

**然后登记完踩到一个必炸的窗口。** 同事那台 `teach enable` 之后，`self` 扫描注册出来的区间是

```
start_ms=1793958567108  end_ms=1793958567354   → 246 毫秒
```

为什么只有 246 毫秒：起点下界是「启用那一刻」（`enabled_at_ms`），首次注册发生在启用后
不到一秒，`scan_bounds` 的 floor 就把窗口压到了两百多毫秒。而搜索的时间只到**秒**
（`transport.search` 用 ISO 秒，带毫秒会被拒），246 毫秒截断成同一个时刻 → 飞书拒
（`start_time must be earlier than end_time`）。

真正致命的是**它看起像合法状态**：`_usable_bounds` 只检查 `start < end`，246ms 满足，
于是每一拍都拿同一对边界去问、每一拍被拒，`watermark` 冻住不动 —— 日志里只有 WARNING，
`teach status` 一切正常，表现成"私聊登记了，但发什么它都不回"。这正是 AGENTS.md §1 说的
"静默降级不是修复"：坏的不是那次拒绝，是"被拒还继续用这对边界"。

**根治（三处，一起去掉那个循环）**

1. `MIN_SCAN_SPAN_MS = 1000`：窗口不许窄于 1 秒（等于搜索接口的精度）。
2. `_usable_bounds` 改成 `end - start >= MIN_SCAN_SPAN_MS`：已经被写坏的老库，下一拍照样重算 —— 不用人工清游标。
3. 注册分支算出不足 1 秒时**这一拍不注册也不搜**（返回 `skipped`），collector 当作"还没到点"、不计失败次数；
   下界不动，所以仍然不回填启用之前的私聊历史。

**判据（可复跑）**

```bash
python3 -m pytest -q                                  # 1065 passed
python3 -m pytest tests/test_repo_hygiene.py -q       # 3 passed
python3 -m pytest tests/test_service.py -q -k SelfScan    # 含"246ms 不拿去问"与"启用后第一拍先跳过"
python3 -m pytest tests/test_transport.py -q -k SelfChat  # 认会话：认得出 / 分页 / 认不出报错 / 多个不猜
python3 -m pytest tests/test_cli.py -q -k Teach           # --self 成功、二选一、认不出拒绝
```

本机反向验证（改完先在真账号上跑，不碰生产配置）：`Transport(config).self_chat_id()`
返回的 chat_id 与 `teach.json` 里登记的教学私聊**完全一致**（真实 id 只在本机，不进 git）。

---

## 30. 私聊通道默认开通：不给技术小白留一条要敲命令的路（2026-11-06 傍晚，本人一句「默认要打开吧」）

**原话**：「默认要打开吧，我们的用户都是技术小白，估计他们连终端命令都不会输入和试用。」

这句话把 §29 的结论推翻了一半：§29 我还在解释「登记是一条命令」，而这条通道的正确默认值就是**开**。
上一版对每台新机器都是死通道：私聊里 @ 自己问问题，一条都进不来、也不报错（同事机 2026-11-06 的
黑五那次就是这么沉默的），而修法要求用户「先去飞书翻一个 `oc_…` 再敲命令」——用户根本做不到。

**改法：没有显式配置 = 默认开。** 服务采集循环每一拍先看一眼，没有 `teach.json` 就自己认出
本人会话（`chat_type=p2p` 且 `p2p_target_id == target_open_id`）写进去，`auto: true` 标明来路：

```json
{"schema":1,"enabled":true,"enabled_at_ms":…,"chat_id":"oc_…","auto":true}
```

三条边界写死在这一步里：① **显式 `teach disable` 一律尊重**，默认值不覆盖本人的决定；
② **配置损坏不覆盖**（看不懂就当作没开，绝不拿一个不认识的文件替本人发言）；
③ **仍不回填历史**，`enabled_at_ms` 是登记那一刻，启用之前的私聊一条都不收。

**唯一的硬前提是权限，所以它必须自己说出来。** 搜索权限（`search:message`）和读正文权限
（`im:message.p2p_msg:get_as_user`）是两套：缺后者时能搜到消息 id、取正文被拒，表现成"通道开着
但一句都不答"。所以缺权限时通道**不跑**，并把原因一路报出去：服务日志一条 warning、
`teach status` 的 `reason`、`doctor` 一条 warn 且直接给出补授权的命令。

**「认不出来」分两种，话必须不一样。** 刚启动几百毫秒认不到会话是正常的（下一秒就好）；
但如果过了 5 分钟（`SELF_CHAT_DISCOVERY_WAIT_S`）还认不出来，那就不是"马上好"，而是**这台账号
还没有"自己跟自己"的会话**——那时候让人干等是错的，得告诉他：在飞书里搜自己的名字点进去
（或「我的空间」）给自己发一条消息，通道随后会自动开通。判定只有一份
（`teach.channel_reason`），服务、`teach status`、`doctor` 都读它，各写一套 = 迟早两个答案。

**判据（可复跑）**

```bash
python3 -m pytest -q                                        # 1073 passed
python3 -m pytest tests/test_repo_hygiene.py -q             # 3 passed
python3 -m pytest tests/test_service.py -q -k SelfScan      # 默认开通/显式关闭不被覆盖/缺权限不开/等 5 分钟改口
python3 -m pytest tests/test_teach.py -q -k ChannelReason   # 六种原因互斥且顺序正确
python3 -m pytest tests/test_cli.py -q -k Doctor            # doctor 对每种原因给下一步
```

干净 home 实测（不碰生产：临时目录 + 真账号只读接口）：

```
teach.json 存在? False → 认会话 → teach.json 存在? True
{"auto": true, "chat_id": "oc_…（与生产同一个会话）", "enabled": true, "enabled_at_ms": …}
```

也就是**没有任何命令**参与：服务起来自己认出来、自己写进去、自己开始扫。

#### 踩坑 15（2026-11-07）：Inno 的 `Add('提示', '默认值')` 编译通过、双击就崩

`TInputQueryWizardPage.Add` 的签名是 `Add(const APrompt: String; const APassword: Boolean)` ——
第二个参数是**「这是不是密码框」**，不是默认值。传字符串 `Add('Linux 密码：', '')` 时：

- ISCC 编译 **通过**（不报错，连警告都没有）；
- 双击 exe 一开就弹 "Type Mismatch"，向导根本没有机会显示出来；
- `/SILENT` 跑连日志文件都不建，退出码 1 —— 看起来像"exe 坏了"。

默认值要用 `Values[Index] := '…'` 赋。真机定位靠一个只写轨迹文件的探针安装包
（`SaveStringToFile` 逐句记录 + 每句独立 try/except 打 `GetExceptionMessage`），
一次就指到 `Add(prompt, string) → Type Mismatch`，`Add(prompt, True)` 同一位置正常。

顺带两条同源事实：
- `Page.Edits` 是带索引的属性数组，**没有 `.Count`**；想数条数得自己数，别写 `Edits.Count`（编译期报 "Invalid number of parameters"）。
- 结论写成了 `tools/windows/installer/smoke-test.ps1`：`[Code]` 改动后必须真跑一次
  `/SILENT /LOG`（用 SYSTEM 计划任务起，因为 SSH 会话的进程没有提权令牌，会卡在看不见的 UAC 上），
  日志里出现 "Linux 密码至少 8 位" 这个校验点才算过。**编译通过 ≠ 能跑。**

#### 踩坑 16（2026-11-07）：`dsh web` 一切正常，除了「一切都不工作」

真机验收时问「Windows 上怎么起 DSH 的 GUI」，第一反应是
`wsl.exe -d Ubuntu -u demo -- dsh web` —— 界面能打开，然后调模型 401、自签网关证书报错。
三件事叠在一起：

1. **密钥不在环境里**。`/etc/feishu-bridge/dsh.env` 是 `0600 root`，运行用户 `demo` 读不到；
   那是 D 阶段特意这么设的（密钥不该被非 root 读到）。于是交互式起的 DSH 拿不到
   `DEEPSEEK_API_KEY` / 渠道 key / `NODE_EXTRA_CA_CERTS`，界面能开、模型不通。
2. **`su -` 会把刚导出的变量清掉**。想用 root 读出 env 再切用户，最自然的写法是
   `su - demo -c …`；但 `-` 是 login shell，它重建环境（HOME/PATH 都是对的，正是这点让人
   以为没问题），刚 `set -a; . env; set +a` 的变量全没了。正确写法是 `su demo -c …`
   （不带 `-`）：HOME/USER/SHELL 会切成目标用户，其余环境原样保留。
3. **「能开不能用」不好归因**。401 来自模型调用，不在 DSH 启动过程里；壳体正常让人
   先怀疑 key 本身或网关，而不是怀疑「进程压根没拿到环境变量」。

根治办法是别让人记这套组合拳：D 阶段第 6 步装 `/usr/local/bin/dsh-web`，把
「载入 0600 的 env + 切成运行用户 + 起 web」固化成一条命令，Windows 侧只剩

```
wsl.exe -d Ubuntu -u root -- dsh-web
```

断言写在 D 阶段（`[ -x /usr/local/bin/dsh-web ]` + `grep '^exec su '`），真机判据：
`dsh-web --no-open` 打印 `http://127.0.0.1:3080/?token=…`，Windows 侧带 token 访问返回
HTTP 200（不带 token 401 属正常设计）。说明见 `INSTALL-WINDOWS.md` §6.5。

#### 踩坑 17（2026-11-07）：「ssh 用户名」到底是谁的名字

装完向导会打印一条 `ssh <用户名>@<设备名>`。被问「装完用户名肯定不是 demo 了吧」时才发现，
这台机器上**两个名字恰好一样**，把一组容易混淆的事实挡住了：

| 名字 | 在哪 | 谁校验它 |
|---|---|---|
| Windows 账号（`whoami` 反斜杠后面那段） | Windows 本机账号 | **sshd 只认这个** —— 公钥写进 `C:\Users\<它>\.ssh\authorized_keys` 或用 `administrators_authorized_keys` |
| Linux 用户名（向导第一页填的） | WSL2 里的账号 | `passwd`、`~/.dsh`、systemd 服务 |

原来 `Get-SshTarget` 拿 `Config.LinuxUser` 拼 ssh 命令，两者同名时看不出问题；一旦不同名
（例如 Windows 用户名是中文、向导推导出 `dsh`），那条命令指到的是**不存在的 Windows 账号**，
同事把命令发给管理员，管理员只会看到 `Permission denied`。

改成用 `$env:USERNAME`（安装会话的 Windows 账号）拼，并在 `apply.Tests.ps1` 里钉住
「Linux 用户名与 Windows 账号不同名时仍用 Windows 账号」。

同源的另一个泄漏：向导默认值被 `/DDefaultLinuxUser=demo` 写死成**测试机**的账号名，
别人装的时候第一页预填的就是别人的名字。改成按 Windows 用户名推导
（`.iss` 的 `DefaultLinuxUserFromWindows()`：小写、只留 `a-z0-9_`、不以数字/`_` 开头，
推不出就 `dsh`），并把推导结果写进安装日志（真机实测：SYSTEM 计划任务跑出来的日志里是
`向导默认 Linux 用户名：system`）。

#### 踩坑 18（2026-11-07）：装完 `dsh web` 能用，官方桌面端却报证书错/401

被问「证书和 key 是不是同时支持 `dsh web` 和 `dsh desktop`」时才发现，这两者读的是**两套东西**：

- `dsh web` 在 WSL 里：证书走 WSL 系统 CA + `NODE_EXTRA_CA_CERTS`（`/etc/feishu-bridge/dsh.env`），
  key 也在那个 0600 文件里 —— 这套是向导 D 阶段装好的。
- 官方 DSH 桌面端是 **Windows 原生**（自带 node/runtime）：Windows 证书库它信（Chromium 窗口没问题），
  但 **node 不读 Windows 证书库**，只认 `NODE_EXTRA_CA_CERTS` 指向的 PEM；key 更是完全读不到
  WSL 里那个 root-only 文件。

真机判据（Windows 侧 node v26.13.0 直连 `https://<网关>/v1/models`）：

```
不带 NODE_EXTRA_CA_CERTS : ERR SELF_SIGNED_CERT_IN_CHAIN
带   NODE_EXTRA_CA_CERTS : HTTP 401        ← 证书通了，只差 key
```

修法：`trust-ca.ps1` 增加 `-WindowsPemPath`（顺手导出 PEM），D 阶段在 WSL 装完后调
`Set-WindowsDshAccess` 补三样：用户级 `NODE_EXTRA_CA_CERTS`、用户级
`DEEPSEEK_API_KEY`/渠道变量、`%USERPROFILE%\.dsh\profiles\web\cordis.patch.yml`
（**这一句后来被踩坑 19 推翻**：桌面端读的是 `profiles\desktop`，写 `web` 对它无效）。
装完会就地跑一次 node 探测（`WIN_NODE_HTTP=401` 才算证书这关过了）。

教训：**「同一个 DSH」在 Windows 上可能是两个进程模型**（WSL 里一套、原生一套），
凡涉及证书/环境变量/key，两边都要给判据，不能因为 `dsh web` 绿了就宣布"装好了"。

#### 踩坑 23（2026-11-10）：wsl.exe 的告警被当成数据 → 把 /etc/wsl.conf 写坏

同事机器（`5CD43246LZ`）上 `setup.exe` 反复卡在同一处：

```
[fail] 建完用户后仍探测不到普通用户（uid>=1000）：/etc/wsl.conf 的 [user] default=wsl: …
```

现场体检（`-Mode PrereqCheck` + 手工探针）给出的事实：

```
default-user : wsl -d Ubuntu -- id -un        → wsl: 检测到 localhost 代理配置，但未镜像到 WSL。…
                                              → root
root-bash    : wsl -d Ubuntu -u root -- echo  → wsl: Expected '=' in /etc/wsl.conf:1
                                                wsl: Expected '=' in /etc/wsl.conf:2
                                                wsl: Invalid escaped character: '…' in /etc/wsl.conf:8
                                                root-ok
wsl -l -v    : Ubuntu-24.04 Running 2 / Ubuntu Stopped 2
```

链条（全是我们自己的 bug）：

1. 这台机器配了 localhost 代理，WSL 3.0.3.0 **每次调用**都往 stderr 多写一行
   `wsl: 检测到 localhost 代理配置……`；而 `Invoke-Wsl` 把 stderr 并进了 `Output`。
2. 离线导入的发行版里本来没有 uid>=1000 的用户 → `awk` 输出为空 → `Output` 只剩那行告警
   → `Get-WslRegularUser` 把**告警文本当成用户名**返回。
3. `New-WslOfflineUser` 看到「用户已存在」→ **跳过建用户**（`pc` 从没被创建）。
4. `Set-WslConfSystemd -DefaultUser <告警文本>` → 把中文告警写进 `/etc/wsl.conf` 的
   `default=`。WSL 只吃 ASCII，于是整份文件解析失败：`Expected '='`／`Invalid escaped
   character`，并且 `[user] default` 从此永久无效（默认用户永远是 root）。
5. `Test-DistroConfigured` 用 `id -u` 的合并输出做 `[int]::TryParse` → 拿到告警文本 → 失败
   → 报「建完用户后仍探测不到普通用户」→ exit 2。

**重跑一次会再写坏一次**，所以这台机器不可能靠重跑旧包救回来 —— 必须换包。

修法（根治，不是加判断）：

- `Invoke-Wsl` 的 `Output` 里剔掉三种噪声前缀（`ConvertFrom-WslOutputLines` 纯函数，有单测）：
  `wsl: …`、`WSL: …`、`<3>WSL (415 - Relay) ERROR: …`。**stderr 不是数据**：实测 WSL 3.0.3.0
  在默认用户不存在时先吐一行内部中继报错，格式不是 `wsl:` 前缀，只按一种前缀过滤会漏。
  抛错信息仍用原始输出，诊断信息一个字不丢。
- 所有「解析 wsl 输出」的地方都过一遍：`Test-DistroConfigured` 只认纯数字 uid；
  `Get-WslRegularUser` / `Resolve-LinuxUser` 只接受合法用户名（`Test-LinuxUserName`，
  `-cmatch`，大小写敏感）。
- `Set-WslConfSystemd` 拒收非法 `-DefaultUser`；并在写之前用 `ConvertFrom-WslConfLines`
  把解析不了的行剔掉（备份成 `/etc/wsl.conf.broken-<时间戳>`），所以**已经被写坏的机器
  重跑一次就自愈**：干净 wsl.conf + 建出 `pc` + systemd。
- 测试钉住：告警行过滤、用户名校验、坏 wsl.conf 的过滤（含「段头之前的键值行」）。

第二次真机失败（同日 17:45，报告里 `default=pc`）暴露了另外两个缺口，一起补了：

- **刚 `wsl --terminate` 就断言**：一次 `id -u` 定生死，而重启（systemd 首启）是秒级到几十秒级。
  现在 `Wait-DistroConfigured` 轮询最多 60 秒；超时也不再说「可能没生效」这种猜测，而是把
  最后一次 `id -u` 的原始输出与退出码、`/etc/wsl.conf` 里的 default/systemd 行、
  `id -un`、`getent passwd` 的实际结果一并写进日志。
- **报告里没有版本信息**：三轮失败后才发现无法确定同事跑的是新包还是旧包。
  `Get-InstallerFingerprint`（文件名 + 文件时间 + sha256 前 12 位）现在出现在报告头和日志首部。

开发机复跑（WSL 3.0.3.0，`<用户>@<开发机>`）：把 `/etc/wsl.conf` 换成上面那份坏文件后

```
before_raw_lines=3   wsl: Invalid key name / <3>WSL (293 - Relay) ERROR: getpwnam(wsl: …) / root
before_Test-DistroConfigured=False
state=changed        （丢了 2 行解析不了的，备份 /etc/wsl.conf.broken-20261110-162959）
after_raw_lines=1    demo
after_Test-DistroConfigured=True
```

即：同一份坏文件，旧代码一直判「没有普通用户」，新代码一次点修好 —— 真机上因此不需要手工改文件。

教训：**捕获原生命令输出时，stdout 和 stderr 混在一起的那一刻就已经错了。**
把「解析失败」留到后面用 `TryParse`／正则兜住，等于让错误数据一路流到磁盘上。
#### 踩坑 22（2026-11-10）：probe 输出为空，被解释成「缺 11 个依赖包」

同事机器的 `-Mode PrereqCheck` 报告（退出码 7）：

```
| E 体检 | fail | WSL 的 PID 1 是「」不是 systemd；Linux 用户 uid=；缺少命令 python3；缺少命令 sqlite3；
                 缺少命令 curl；缺少命令 git；缺少命令 npm；缺少命令 lark-cli；Node 版本「」低于 18；
                 lark-cli 不能执行；Windows sshd 未运行；Windows 22 未提供 SSH banner |
```

十项「缺失」里没有一项是真缺失：`pid1`、`uid`、`node_version` 全是空字符串，说明
`wsl -d Ubuntu ... bash` **根本没有输出**（probe 没跑起来），而 `ConvertFrom-PrereqProbe`
把「一个字段都没拿到」按「每一项都不合格」逐条展开。读报告的人会去查 apt 源／Node，
而真正的毛病在 A 阶段（发行版没起来，或没有普通用户）。

修法（同样不改安装行为，只让报告说实话）：

- `ConvertFrom-PrereqProbe`：没有任何 PREREQ 行时只报一条「探测不到 WSL 内部状态」。
- `Install-PhasePrereqCheck` 加三道前置门，各自点名层级并返回 2：
  1. 读不到发行版列表（`wsl --status` / `wsl -l -v`）；
  2. 目标发行版不在列表里（列出已有的）；
  3. 发行版在、但 `-u root` 也跑不起 bash（虚机/内核/初始化问题）；
  4. 再以目标用户跑 probe，跑不通时用 root 查 `id -u <user>`，把「该用户不存在」和
     「用户存在但 shell 不可用」分开报。
- 测试：空 probe 只报一条；四种失败各自返回 2 且 fail 行点名对应层级。

教训：**「没数据」和「数据是坏的」是两件事**。把「探测失败」渲染成「十一项不合格」，
比不给报告更糟——它指错了方向。
#### 踩坑 21（2026-11-10）：退出码 2 的报告是一张空表 —— 没说停在哪一步，也没有日志

同事的机器跑 `setup.exe`（实际是从手工解包的目录跑 `install-windows.ps1 -Mode Prereq`），
发回来的报告长这样：

```
Windows 前置资源安装报告（-Mode Prereq）
- 退出码：2
| 阶段 | 结果 | 明细 |
| --- | --- | --- |
（下面什么都没有）
```

三个问题叠在一起：

1. **退出码 2 只说明「A 阶段没过」**（WSL 组件 / 发行版 / systemd / Linux 用户这一层），
   具体是 DISM 失败、缺 WSL MSI、发行版导入失败、还是没有普通用户，报告里一个字都没有。
2. **阶段表空**：`Add-PhaseResult 'A 环境' 'ok'` 只在**成功时**记一行，A 阶段中途返回 2
   就一行都没有 —— 读报告的人分不清「没跑到」和「跑了但没成」。
3. **没有任何日志**：`install-windows.ps1` 只往控制台打印，窗口一关现场就没了
   （`setup.exe` 那条路有 `install.log`，但直接跑子脚本时没有）。

于是这次只能靠人问「你控制台最后几行是什么」，而同事的窗口早关了。

修法（报告与日志补齐，不改任何安装行为）：

- `Write-Err` 记住最后一条错误；每个阶段开始前登记 `$script:CurrentPhase`。
- 失败时若阶段表里没有 fail 行，就补一行 `| <阶段>（在这步中断） | fail | <最后一条错误> |`。
- 报告新增「## 停在哪一步」小节：退出码 + 它的含义（`Get-ExitCodeMeaning`）+ 最后一条错误 + 日志路径。
- 每条输出同时写进 `%ProgramData%\feishu-kit\logs\install-windows-<时间>-<模式>.log`。
- 测试钉住：退出码含义、`Write-Err` 记录与落盘、失败报告必须出现中断阶段名/失败原因/日志路径、
  成功报告里不许出现这些失败专用内容。

教训：**报告是给下一个人的唯一现场**。只写「失败」不写「停在哪」的报告，等于把定位成本
推给一个不在现场的人；而退出码这种东西，必须在报告里就地翻译成「哪一层」。
#### 踩坑 20（2026-11-09）：需要重启的机器，重启后就再也装不完了

同事机器上的原始报错（setup.exe 跑到一半，自动重启后由 RunOnce 续跑）：

```
feishu-kit 安装器开始：2026-11-09T17:59:54
安装中止：无法使用项 C:\ProgramData\feishu-kit\install-config.json 其自身覆盖该项。
位置：apply.ps1:610 字符: 5      ← Copy-Item -LiteralPath $SourcePath -Destination $target -Force
INSTALL_FAILED 1
```

原因：`Save-ConfigSnapshot` 把「向导给的配置」复制成 `C:\ProgramData\feishu-kit\install-config.json`
供重启后续跑用；而续跑时 RunOnce 传进来的 `-ConfigPath` **就是这份快照自己**，于是
`Copy-Item` 源=目标，抛「无法使用项 … 其自身覆盖该项」。它位于 `Invoke-Main` 最前面，
所以续跑一启动就中止；更糟的是 RunOnce 已被 Windows 用掉，机器就停在半装状态，
同事只能看着窗口报错、不知道下一步做什么。

判据（dev 机复现，可重放）：把 state.json 写成四个阶段全 done、`install-config.json` 当配置，
用旧代码跑 `apply.ps1 -ConfigPath <快照> -ResumeState` → exit 1 + 同一句报错；
换成修好的代码 → 跳过已完成阶段、`INSTALL_OK`、exit 0。

修法：新增 `Test-SamePath`（大小写不敏感），源=目标时不复制。
`apply.Tests.ps1` 钉住四件事：同一文件不报错、返回它自己、不同路径照常复制、大小写不同算同一个文件。

教训：**「续跑」这条路径必须有真机判据**。前面所有测试都只覆盖「第一次装」，
四阶段串跑在真机上绿了不等于重启分支绿了 —— 而分支机构恰恰是 WSL2 第一次安装最常见的走向。

#### 踩坑 19（2026-11-09）：装完桌面端还是连不上 —— 配置写进了 `web` profile，桌面端只读 `desktop`

发布前做真机验证时发现的（不是猜的，步骤可重放）：

1. 装机目录 `C:\Users\demo\AppData\Local\Programs\DeepSeek Harness\`，静默装（`/S`）成功，
   版本 `0.2.2-rc.1`；
2. 第一次启动桌面端，它自己建了 `%USERPROFILE%\.dsh\profiles\**desktop**\`
   （`cordis.patch.yml` 开头是「Your patch layer for this dsh profile」），
   同时 `profiles\web` 下我们写的渠道配置**没有被读**；
3. 判据（两条，都指向同一个结论）：
   - `dsh --profile desktop --dump-config` → `error: profile "desktop" is managed
     exclusively by the Electron application`（CLI 根本不管这个 profile）；
   - 同一段渠道配置：`dsh --profile web --dump-config` 里有 `acme-ai-channel` /
     `<网关地址>` / 默认模型；`desktop` 那份 dump 里一个都没有。
4. 而两个 profile 的 `package.json` 的 bundle 栈完全一样（`@deepseek-ai/dsh-base` +
   `@deepseek-ai/dsh-web-app`）—— 差的只是「桌面端读哪个目录」。

所以 `Set-WindowsDshAccess` 原来写 `profiles\web\cordis.patch.yml` 是**静默无效**：证书和 key
都到位了，桌面端却看不到任何自建渠道，等于「装完一键用」没兑现。

改法：写 `profiles\desktop\cordis.patch.yml`（机器上还有 `web` 就两处都写）；并且改成
**标记段合并**（`# >>> feishu-kit` / `# <<< feishu-kit`）而不是整份覆盖 —— 桌面端那份文件里
有它自己的 `ui-chat` / `ui-settings` 条目，覆盖等于清掉用户设置，重跑还会互相抹。
两个 profile 目录都不存在时才走待用文件（说明并进 `profiles\desktop\cordis.patch.yml`）。

顺手记一条环境坑：PowerShell 5.1 在中文 Windows 上按 GBK 读**没有 BOM** 的 `.ps1`，中文一多
就在引号处报「字符串缺少终止符」。这次用编辑器改 `apply.ps1` 就把 BOM 弄丢了，Windows 上整份
脚本解析失败。已加门禁 `tests/test_repo_hygiene.py::WindowsScriptsKeepBom`
（被跟踪的 `.ps1` 只要含非 ASCII 就必须有 BOM）。

#### 踩坑 21（2026-11-09）：装完桌面端起不来 —— 我们的 patch 接在了桌面端那行 `[]` 后面

同事报「dsh 的 desktop 起不来」。桌面端日志（`%APPDATA%\@deepseek-ai\dsh-desktop\logs\crash-*-host.log`）：

```text
source: host   phase: startup   app: @deepseek-ai/dsh-desktop 0.2.2-rc.1   electron: 44.7.0

DesktopHostFatalError: dsh: failed to parse overlay C:\Users\<user>\.dsh\profiles\desktop\cordis.patch.yml:
YAMLException: end of the stream or a document separator is expected (9:1)

  6 | # >>> feishu-kit 安装程序写入（重跑会整段替换这一段）
  9 | - id: llm-pi-ai
------^
 10 |   config:
```

现象：Electron 主进程 + gpu/network/renderer 都在，但**没有窗口**（`MainWindowTitle` 全空），
每隔十几秒重起一次，每次都在 startup 阶段多一份 crash log —— 从用户视角就是「双击没反应」。

原因：桌面端第一次启动建出来的 `profiles\desktop\cordis.patch.yml`，正文只有一行 `[]`
（外加三行注释头）。`Merge-DshProfilePatch` 找不到我们的标记段时是**追加到末尾**，于是文件变成
「`[]` 把文档结束掉 + 后面又一个 `- id:` 根节点」—— YAML 一个文档只能有一个根，解析直接抛。
`loadProfileDirectory` 在 host 启动路径上，所以整个桌面端起不来。
（同一批另一台同事机没坏，是因为它的 profile 里桌面端已经写过自己的 `ui-*` 条目，
根早就是块序列，追加是合法的。差别只在「这个 profile 用过没有」。）

修法（`tools/windows/installer/apply.ps1`）：

1. 合并前先删掉**单独的** `[]` 行 —— 那是空数组根，我们的条目应当顶上去当根序列；
2. 落盘前再按 YAML 语法判一次根形状（新增 `Get-DshPatchRootKind`：
   `empty` / `empty-flow` / `sequence` / `other`），不是 `sequence` 就抛错、**一个字节都不写**，
   D 阶段报 fail 并打印「把它改成 `[]` 或删掉后重跑」；
3. 标记段替换那条路走同一套判断 —— 所以**旧版本已经写坏的机器，重跑一次安装就能自愈**
   （`[]` 被清掉，标记段换成正确条目）。

判据（可复跑）：

```bash
powershell -NoProfile -File tools\windows\installer\apply.Tests.ps1
```

56 项全过，其中 11 项是新钉的：`[]` 根能合并、合并结果里不再有 `[]`、根是 `[]` 的历史坏文件
能被治好、根是映射时抛错不落盘、空文档/块序列/`[]`/映射四种根形状判定正确。
真机判据（同事那台，桌面端 0.2.2-rc.1）：修前每次启动都 `DesktopHostFatalError` + 无窗口；
只改这一份 yml、同一个 exe 再启动 → `dsh-desktop-host` 子进程在位
（`--expose-internals …\dsh-desktop-host\lib\index.js`）、`logs\` 里不再新增 `crash-*-host.log`、
renderer 正常加载。

教训：**「往 YAML 里加一段」不是文本操作，是文档结构操作。** 我们的合并只考虑了「文件里已有条目」
和「文件空 / 只有注释」，漏了「文件有一个空数组根」这种**看起来空、其实已经把文档结束掉**的形状。
凡是往别人的配置文件里写东西，写完都要按目标格式的语法判一次，不能只看字符串拼得整整齐齐。

## 31. 群里那句「好的，宝哥」：AI 替本人说话时把自己当成了对话对象（2026-11-10 上午，本人报"回复很奇怪"）

**原话**：「我的 mac 本机上的 bridge agent，在我们这个群里的回复很怪……别人@我发的信息，然后，
bridge agent 回复的时候，又@我，回复了一个好的，很奇怪。」（群 `oc_c462…`）

先按证据把"怪"拆成三件事，只有一件是显示错觉：

| 现象 | 真相 |
|---|---|
| 回复"又 @ 我" | 回复是**引用**（`reply_to`）那条 @ 本人的消息；飞书把被引用的原文（含 @本人）一起渲染出来。回复正文里**没有** @ 任何人的标签（`+messages-mget` 实测 `sender=本人`、无 mentions 字段） |
| 回复内容「好的，宝哥。有需要随时跟我说，不用客气。😊」 | 真的发出去了（outbox 原文可查）。两处错：① 正文是对**本人**说话的口吻（"宝哥"），可这条消息是用**本人账号**发给同事的——等于自己管自己叫宝哥；② 对方那句原文是「@本人 哦哦」（`parent_id` 指向前一条「不去啦」），纯承接，不需要答 |
| 更早的历史 | 同一个病在群里犯过多次：`oc_9959…` 里 2026-11-07 起有多条回复以「宝哥，…」开头；2026-11-07 19:39 还有一条把消息里的占位符 `@_user_1` **原样抄进了正文** |

**根因两条，都是"缺一条事实"，不是模型笨：**

1. **prompt 里没有"群场景"这一段。** 事件载荷只有 `text`，没有 `chat_type`、没有 `@_user_1`→人名的映射表。
   模型看到的是 `"@_user_1 哦哦"`，只能猜；它知道自己是"某人的 AI 助手"，于是把"称呼宝哥"这条教材
   用在了**替本人对外说话**的场合——教材的适用域（只在对本人说话时用）从来没写出来过。
2. **管线里没有"不回"这条路。** `events reply` 要求正文非空（`finalize_answer` 拒绝空答案），
   于是每条 @ 都必须说点什么；遇到纯承接，模型只能编一句客套话。

**改法（一条事实一个答案）：**

- `payload.chat_type`（`group`/`p2p`）+ `payload.mentions`（`@_user_1` → 人名，≤20）由采集侧一次算准
  （`service.external_payload`），prompt 直接用，不再让模型猜；
- `agent_prompt._group_section()` 只在 `chat_type=group` 时进入 prompt：**不称呼本人**（教材里的称呼规矩
  只在私聊适用）、**不把占位符抄进正文**、**不客套收尾**、**纯承接就判"不用回"**；拿不准是否在叫他 → 按要回处理；
- 新增「不回」收尾：答复 JSON 只写 `{"skip":"为什么不用回"}` → 运行时改调 `events skip`（CLI 与 HTTP 网关
  `POST /v1/events/{id}/skip` 同一实现）→ `Store.complete_external_skip` 把事件标 done、inbox 记
  `ignored` + `未回复：<原因>`，**不写 outbox、一条消息都不发**；`turns.jsonl` 记 `skipped`；
- 私聊**不**允许 skip（`finalize_reply(allow_skip=False)` 直接失败关闭）：对本人沉默等于失联。
  群消息里 skip 与 answer 同时出现也失败关闭——回不回只能有一个答案。

**判据（可复跑）：**

```bash
python3 -m pytest -q tests/test_agent_prompt.py tests/test_consumer_safety.py \
  tests/test_consumer_escalation.py tests/test_store.py tests/test_cli.py tests/test_gateway.py
# 全量：1107 passed；卫生门禁 4 passed
```

新钉的行为：群消息可 skip（`Store`/CLI/网关/运行时四处都有）、私聊 skip 被拒、skip+answer 报错、
`chat_type`/`mentions` 进载荷、群 prompt 含"不要称呼他/不抄占位符/{"skip"…}"、私聊 prompt 两者都不含。

**第二刀（同一天，本人跟了一句）**：「某人@我，回复的时候，一定是@某人啊，肯定不能@我吧」。

对。而且「@ 谁」不该由模型决定：它要么抄占位符，要么 @ 错人——这次错的就是 @ 本人。
改成**一处决定**（`bridge/reply.py:render_reply`）：

- 群里回复由 bridge 在正文最前面加 `<at user_id="{发消息的人}"></at>`（`payload.sender_id`），
  模型一个字都不用写 @；
- 正文里**任何指向本人的 at 标签一律删掉**（`self_open_id`），占位符 `@_user_1` 也一律删掉；
- 私聊不加 @；提醒正文同样过这道清洗（提醒从不 @ 任何人）。

真机判据（语法必须先验证，否则宁可不发）：2026-11-10 13:31 用本人身份在测试群发
`<at user_id="ou_…"></at> 语法自检`，`+messages-mget` 读回 `mentions:[{key:"@_user_1",name:"…"}]`、
正文渲染成「@某人 …」，随后撤回；说明这条语法在 user 身份下成立、且引用回复不需要它。

**留给本人的一个选择**（没有替他定）：群消息**永远**引用回复（现在这样，回复落地的读回校验依赖
`reply_to`），还是短回复直接发不引用（要改 `verify_reply` 的判据）。前者引用框里仍会显示原消息的
「@本人」字样（不是新 @，也不产生通知），后者连这个框都没有、但"这条是回谁的"变模糊。
现在正文已经明确 @ 提问的人，所以两种都能看清是回谁。

## 踩坑 22（2026-11-09）：模型选择器里只有一个模型 —— 安装程序没问网关

现象（同事那台 Windows，桌面端装好、能启动、能聊）：`deepseek` 的模型列表里只有一个
`deepseek-v4-1-flash-261017`，而同一把 key、同一个网关上其实有 3 个模型。用 key 直接问就行：

```bash
curl -s -H "Authorization: Bearer $ACME_AI_CHANNEL_API_KEY" https://<网关>/v1/models \
  | python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))'
# doubao-seedream-5-0-flash-260915
# doubao-seed-2-0-lite-260428
# deepseek-v4-1-flash-261017
```

原因：`Get-DshProfilePatchYaml`（Windows 侧）和 `install-dsh.sh`（WSL 侧）写 `models:` 时，
都只写了配置里那一个 `Model` —— 「默认模型」被当成了「模型清单」。而模型清单是**网关的事实**，
我们这边根本没有它的副本：能证明「应该有 3 个」的只有网关自己的 `/v1/models`。
（旁证：另一台同事机的 `models:` 里是齐的 3 个，因为有人手工加过；说明这不是网关的问题，
是安装程序少写了。`research/facts-dsh.md` 里那句「4 个模型」也是同一类漂移 —— 抄下来的清单会过期。）

修法（两侧语义一致，实现各写各的）：

1. 新函数 `Get-GatewayModelIds`（`tools/windows/installer/apply.ps1`）：
   `GET <baseURL>/v1/models`（带渠道 key），拿到的 id 就是清单；默认模型不在里面就补在最前面
   （`Merge-ModelIds`：去重 + 补默认模型，顺序仍按网关给的）；
2. `install-dsh.sh` 同一套语义（bash + `curl` + `python3` 解析 JSON），断言阶段逐个模型 id 在
   `dsh --profile web --dump-config` 里有；
3. **取不到就退回只写默认模型，并打 `[warn] 取不到 …/v1/models 的模型清单`** ——
   网关不提供该端点（比如 TokenHub 那类）时会看到这条，静默少给是被禁止的那种「修复」；
4. 仓库/构建配置里**不加**模型列表：`-ConfigJson` 的 `DshModel` 仍然只是默认模型。

真机判据（同事那台，都跑过）：

```powershell
# 1) 网关清单能取到（Windows PowerShell 5.1，证书走 Windows 证书库）
$k = [Environment]::GetEnvironmentVariable('ACME_AI_CHANNEL_API_KEY','User')
(Invoke-RestMethod -Uri 'https://<网关>/v1/models' -Headers @{Authorization="Bearer $k"}).data.id
# → 3 个 id

# 2) 新代码在这台机器上算出来的就是 3 条（dot-source apply.ps1，喂真实配置）
#    discovered count=3 / root=sequence / modelCount=3

# 3) 把桌面端真正加载的那份 patch 喂给同一套 loader，合成结果里是 3 个模型
HOME=<临时目录> dsh --profile web --dump-config | grep -A12 'acme-ai-channel'
```

第 3 条是关键：它证明「文件里的 3 条」会被 loader 合进 provider 的 `models`，
模型选择器就是照这个列表渲染的 —— 不是只看 yml 里写了几行。

教训：**「配置里该写什么」如果是一份会变的事实，就不要抄到自己的脚本/构建配置里。**
这次抄的是模型清单，上次（踩坑 21）是「配置文件的结构假设」。两次的共性是：
我们把**别人的事实**当成了自己的常量，于是它一变，我们的产物就悄悄少东西 ——
而症状（"列表里少几个"）离原因（"安装程序写死了"）隔得很远。


---

## 踩坑 23（2026-11-09 晚）：多轮测试收尾把同事的「上班」改回了「挂起」，96 小时零采集

**本人怎么发现的**：「我看同事 A 今天一天 她的 bridge agent 都没发生，是出了什么问题了吗？她现在 tailscale
是在线的」——网络在线、服务健康，但一整天没有任何反应。

**取证（只读，全部可在她机器重放）**：

- `hold.json`：`active=true`、`since_ms=1793879783752`（2026-11-05 19:56:23）、`by=multi-turn-test`、
  `reason="Mac 侧多轮任务测试结束，恢复原挂起"`——已挂起 **96.2 小时**；
- 今日 `inbox` 新增 0、`events` 新增 0；最近一条 inbox 停在 **2026-11-05 19:55**；
- 日志今天只有 9 行、全是控制通道 DNS 告警；每次启动都打
  `bridge held: collector idle, no search, no queue writes`；
- `doctor --assert` verdict **ok**（`authenticated=valid`、`sending_enabled=true`、`release_match=ok`），
  唯一一条 `mode: info 已挂起（held）：不收消息是有意为之`——**没有故障项，纯粹是挂起**。

**自聊取证（本人授权后读她自己的自聊）**：2026 年自聊共 483 条，其中「上班/下班」指令只有 7 条：

| 时间 | 指令 | 桥的回应 |
|---|---|---|
| 11-02 16:43 / 16:44 | 下班 / 上班 | 已下班（挂起中） / 已恢复接待 |
| 11-04 10:33 | @本人 上班 | —— |
| 11-04 20:50 / 20:51 | @本人 上班 / 上班，收到请回复 | 未处于挂起状态 |
| 11-05 16:45 | @本人 下班 | 已下班（挂起中），开始时间 16:45 |
| **11-05 19:42** | **@本人 上班** | **未处于挂起状态**（已恢复接单） |
| 11-09（当天） | 无任何消息 | —— |

**根因**：19:50–19:51 那一轮多轮任务测试用了她的自聊；**19:56 收尾写 hold 时理由写的是「恢复原挂起」——
它凭记忆把 16:45 那次「下班」当成"原状态"，忽略了她 19:42 已经发过「上班」**。于是她的机器被挂起 4 天，
所有 @ 她的消息按挂起语义**从未入队**（因此也不会补答），而她大概一直以为助手在跑。
这是典型的「凭记忆还原生产状态」：只要记错一次，就是**把别人的服务关掉而无人察觉**。

**修复（20:19，`release`）**：`feishu-bridge release --by ops-restore
-r "还原：11-05 19:42 本人已上班，19:56 测试收尾误写挂起，现恢复接单"`。验证：
`mode=running`、`search_fresh=3.0`、`watermark_lag_s=2.6`、`receiving_healthy=true`、
`consumer status` → `healthy=true`（dsh-main 在领）、`check` rc=0 `RESULT=ok`。
说明：`release` 自身会往她自聊发一条「已恢复接待」状态回执（设计行为，不是我给她发的消息）；
挂起窗口内的消息永久跳过，`dropped_at_release=0`（因为从未入队）。

**根治（不止是文档）**：新增 `tools/state_guard.py` + `tests/test_state_guard.py`（8 条）——
测前 `--save` 存基线（只存 `mode`/`installed_release`/`running_release`/`update_pending`，心跳这类抖动字段
不进基线）、测后 `--compare`，**mode 或 release 变了就退出码 3**，并打印"按基线还原"的那条命令。
规定写进 [docs/ops-remote-manage.md](ops-remote-manage.md) §8：**「还原」= 还原到测试开始前的实际状态（存成文件比对），
不是记忆里更早的状态**。同一节还写了：改状态的测试结束必须再跑一次 `version`/`hold` 判据并留输出。

**连带纠正**：§28 里把 11-06 升级时看到的 `mode=held` 记成"她有意挂起的"，是误判（已加更正）。
**静默升级完成（同日 20:32–20:34，本人要求「静默升级，升级后确保服务状态正常」）**：她机器从
`40dff6dc2223ad29` 升到 `94fd5470da410e2d`（= HEAD `2853c75`，与本机 Mac 同一 id），**全程没有给她发消息**
（升级后自聊里仍只有 20:19 那条 `release` 回执）。判据：

- 发版：`pack-windows.sh` → `dist/windows/feishu-bridge-windows-20261109.zip`，184 文件 / 1.1 MB /
  sha256 `697ac431…`；`repo/bridge/{cli,service,teach,transport}.py` 与工作树逐字节相同，非 ASCII 名全带 UTF-8 标志位；
- 升级前先按 §3 拍快照 `state-v7-pre-upgrade-20261109-2032.sqlite3`（integrity ok、sha `bd5555d0…`）；
- `upgrade` rc=0：`MIGRATE clean_count=0`、消费者 `ActiveEnterTimestamp 11-06 15:41:27 → 11-09 20:33:19`（自证重载）、
  `FINALIZE_INSTALL=ok`；
- `version` → `installed==running==94fd5470da410e2d`、`update_pending=false`、`mode=running`；
- `check` rc=0 `RESULT=ok`（`release_match=ok`、`consumer_release=ok`、`search_fresh≈8s`）；
- `doctor --assert` verdict ok（唯一 warn 是历史 dead 事件）；`consumer status` → `healthy=true`（dsh-main 0.2s）；
- 采集：`watermark_lag_s=0.6`、`receiving_healthy=true`、`consecutive_failures=0`（读链路真的在跑）；
- 库：`user_version=7` 不变（本次无迁移）、inbox/outbox/events = 127/59/109 与升级前一致、integrity ok；
- 发送链路：`notify --dry-run` → `decision: send`（**没有真发**，避免打扰）；真机 send 自 11-05 起没再实测过；
- Windows PS 套件：`WindowsDistribution.Tests.ps1` 190 项 + `InstallWindows.Tests.ps1` 113 项全过（rc=0）。

顺带用新工具做了升级前后比对（`state_guard --compare`）：`mode` 一致、只有 `installed/running_release` 变了，
正是升级该有的样子；这次也暴露了它的一个提示缺陷——release 变了却打印"跑 `hold/release` 还原"，
把人往挂起开关上引。已修：只有 `mode` 变了才提示开关，纯 release 变化只提示"确认是有意升级"。

**仍待观察**：控制通道偶发 DNS 解析超时（11-06 16 次、11-07 21 次、11-08 1 次、11-09 9 次；实测当前解析正常），
若她发「上班」撞上失败窗口会不生效；4 天里丢掉的 @ 消息不会补答，需要她重发。

**一周后再对齐一次（2026-11-16 15:39–15:41，本人要求「务必让她的 bridge agent 和我版本一致，且服务状态正常」）**：
她 11-10 10:34 自己发「下班」挂起（`by=feishu`），11-16 15:30:40 自己发「上班」恢复（`by=feishu`）——这一周是她本人的选择，
不是故障；恢复后采集立即正常（`search live hits=1`，抓到一条 `@_all` 消息并按策略 `ignored`）。
但她的 release 还是 11-09 装的 `94fd5470da410e2d`，而本机 Mac/HEAD 已是 `a24c76ab53ebb285`（差 13 个提交，
其中两个动了运行时：`4550f83` 群里回复 @ 提问的人绝不 @ 本人、`059c83b` 群里不许自称"宝哥"/不抄 `@_user_1`）。
于是再做一次静默升级：

- 升级前：`state_guard --save` 存基线（`mode=running`）；`VACUUM INTO` 快照 `state-v7-pre-upgrade-20261116-1539.sqlite3`
  （integrity ok、sha `c4cc8f78…`）；
- 包：`dist/windows/feishu-bridge-windows-20261116.zip`，185 文件 / 1.1 MB / sha256 `a40b96fb…`；
  `bridge/{reply,store,gateway}.py`、`consumer_runtime.py`、`agent_prompt.py`、`install.py` 与工作树逐字节相同，非 ASCII 名全带 UTF-8 标志位；
- `upgrade` rc=0：`MIGRATE clean_count=0`、消费者 `ActiveEnterTimestamp 11-09 20:33:19 → 11-16 15:40:05`、`FINALIZE_INSTALL=ok`；
- **两端同版本**：她 `installed==running==a24c76ab53ebb285`、`update_pending=false`；本机 Mac 同样
  `installed==running==a24c76ab53ebb285`、`update_pending=false`（`release.release_id('.')` 与两台机器三方一致）；
- `check` rc=0 `RESULT=ok`；`doctor --assert` verdict ok（唯一 warn 仍是历史 dead 事件 21 条）；
  `consumer status` → `healthy=true`（dsh-main 2.9s）；`collector.watermark_lag_s=0.8`、`receiving_healthy=true`、`consecutive_failures=0`；
- 库：`user_version=7` 不变（本轮无迁移）、inbox/outbox/events = 133/63/113 与升级前一致、integrity ok；
- `state_guard --compare`：`mode` 保持 `running`，只有 `installed/running_release` 变了（升级该有的样子）；
- Windows PS 套件：`WindowsDistribution.Tests.ps1` 190 项 + `InstallWindows.Tests.ps1` 173 项全过（rc=0）；
- 静默确认：升级后自聊没有新增任何 bridge 发出的消息（15:30 那条「已恢复接待」是她自己「上班」的回执）。
- 未做：升级后还没有一条真实 @ 她的消息走完端到端（只有一条 `@_all` 被正确忽略），发送链路的真机 send 自 11-05 起仍没实测。

**配置也对齐（同日 16:03，本人要求「配置也和我保持一致」）**：逐键比对后只有一处行为差异——她的
`live_interval_seconds` 是 **12**（本人机器 3），于是采集每轮慢 3 倍、求助/回复的拾取也跟着慢。
改成 3（先备份 `backups/config-pre-interval-20261116-160341.json`，原子写保留 0600），重启 bridge
（`ActiveEnterTimestamp 16:03:46`，消费者不动，仍是 15:40:05 那次的版本），实测 `search live` 节奏从
12–13s 一轮变成约 4s 一轮（3s 间隔 + 1s 最小搜索间隔 + 请求耗时），`mode=running`、心跳 1.2s。

比对结果：9 项行为配置全部一致（`app_daily_reply_cap`、`artifact_retention_days`、
`backfill_interval_seconds`、`collector_backoff_seconds`、`live_interval_seconds`、
`release_id`、`search_min_interval_seconds`、`self_scan_seconds`、`sending_enabled`）；
剩下的差异都是**按人**的，不该抄：`target_open_id`、`signature`（署名带她自己的名字）、
`self_chat_ids`、`lark_binary` 路径。

**顺带更正一个误判**：我先前说她"没有 `consumer_workspace`，agent 只能在私有目录干活"——错了。
`consumer_workspace` 是 **Cursor 适配器专用**的键（`agentkit-consumer.py --add-dir`），DSH 的工作目录写在
systemd unit 的 `--workspace`；她机器本来就是 `~/dsh-workspace`（属主是她自己、可写，里面已有
`list_export.csv`/`shot.jpg` 这类她自己的产物）。所以这一项既不用改、也不该往 config 里抄一份——
抄了就是同一事实两处定义。

**判据**：`check` rc=0 `RESULT=ok`、`doctor --assert` verdict ok、`consumer status` healthy
（dsh-main 0.4s）、`version` 仍 `a24c76ab53ebb285`（改配置不影响 release id）、库 `v7` /
`133/63/113` 不变、`integrity ok`。
**一个要记住的语义**：`config.json` 只在进程启动时整份读取；`sending_enabled`/`target_open_id` 每轮复查
（发送门禁），其余键（含 `live_interval_seconds`、`self_scan_seconds`）**改了必须重启 bridge 才生效**。

## 32. 只装证书：`install-gateway-cert.ps1`（2026-11-10 傍晚，`setup.exe` 全线失败后的收缩）

同事机器的 `setup.exe` 换到第 6 个包仍在 A 阶段失败，本人决定放弃整套安装包，**只留证书**。
教训不是"再多修一个 bug"，而是交付物太大：只要同事的 `setup.exe` 一失败，连最基础的
"让这台 Windows 信任内网网关"都拿不到。所以证书安装从安装包里独立出来，变成一个单独文件，
不依赖安装包目录、不依赖 WSL、不需要仓库。

真机（Windows 11 26200 / PowerShell 5.1 / 真网关，地址见本机 `.gitignore` 配置）正反跑通后，踩到四个只有真跑
才会暴露的问题（全部已修 + 写进测试 + 在脚本注释里注明原因）：

1. **`CurrentUser\Root` 的 `Add` 返回「不支持该请求」（ERROR_NOT_SUPPORTED）**，同一台机器
   `LocalMachine\Root` 却成功。所以安装顺序改成**机器库优先**（管理员），用户库只作非管理员退路；
   两处失败的话术分开说，不合并成一句"装不上"。
2. **从 TLS 链里取出的 `X509Certificate2` 带着握手上下文**，直接 `X509Store.Add` 抛
   NotSupportedException。入库前必须用 `RawData` 再造干净副本（`New-ContextFreeCertificate`）。
   这个坑之前在安装包里没暴露，是因为那时证书**早就在机器库里**（打印"已信任，不重复导入"），
   `Add` 从来没被真正执行过。
3. **`curl.exe` 报 `CRYPT_E_NO_REVOCATION_CHECK`**：内网 CA 不发布 CRL/OCSP，schannel 把
   "查不到吊销状态"当失败；同一时刻 .NET 和 node 都是通的。探测必须带 `--ssl-no-revoke`，
   并且把该错误记为"TLS 通过"。
4. **`Get-TlsChain` 用 `return ,@($certs)` 返回**时，调用方管道过滤会拿到 `object[]`，
   进而报「无法将 System.Object[] 转换为 X509Certificate2」。改成返回
   `[pscustomobject]@{ Certs = … }`：显式比"聪明"的数组技巧可靠。

正反验证（`-DryRun` 就是为这件事存在的：同一个脚本打印装前/装后）：

```
# 卸载后 —— 与同事机器现状一致
CURRENTUSER_TRUSTED=False
DOTNET_TLS_BEFORE=ERR=根据验证过程，远程证书无效。
CURL_TLS_BEFORE=HTTP=000
NODE_TLS_BEFORE=ERR=SELF_SIGNED_CERT_IN_CHAIN
# 装完
DOTNET_TLS=HTTP=401    CURL_TLS=HTTP=401    NODE_TLS=HTTP=401    CERT_RESULT=ok
```

**DeepSeek 桌面端（Electron）为什么两处都要**：窗口里的请求走 Windows 证书库（Chromium/schannel），
主进程的 Node 侧**完全不读系统证书库**，只认 `NODE_EXTRA_CA_CERTS` 指向的 PEM。
少任一处，表现就是"界面能开、一问模型就报证书错误"。而且环境变量只影响**新进程**，
所以脚本在设完变量后广播 `WM_SETTINGCHANGE`（让 Explorer 重读，不必注销），
并明确告诉用户"完全退出桌面端重开；仍不行注销重登"。

**已知边界（没验证过的别写成已验证）**：开发机没有交互式桌面会话、也没有真实渠道 key，
所以"桌面端窗口里真的会调模型"这一步没有被观测过；被观测到的是三条 TLS 探针（.NET/curl/node）
在装前失败、装后 401，以及用户级环境变量确实写进了注册表。

## 33. 「禁止运行脚本」不是脚本坏了：执行策略 + 双击启动器（2026-11-10 夜）

同事拿到 `install-gateway-cert.ps1` 后报「禁止运行脚本」。开发机上 `-ExecutionPolicy Restricted`
一次就复现了同一句报错（`无法加载文件 …，因为在此系统上禁止运行脚本`）——原因不是脚本，是
**PowerShell 默认执行策略**：双击 `.ps1`、或只敲 `.\install-gateway-cert.ps1`，都会被打回。
更麻烦的是公司机器常由组策略下发 `MachinePolicy = AllSigned/Restricted`，那时连
`-ExecutionPolicy Bypass` 都会被策略覆盖。

修法不是"再写一段说明让同事记得加参数"（说明一定会被跳过），而是**把入口换成双击**：

- `install-gateway-cert.cmd`（GBK + CRLF，中文 cmd.exe 才不会乱码）：自己申请管理员
  （`net session` 判断 → `Start-Process -Verb RunAs`）、没带参数就问网关地址、
  **把 .ps1 读进内存 `[scriptblock]::Create(...)` 执行**（内存执行不受执行策略约束）、
  结束前 `pause` 停窗。
- `.ps1` 仍是唯一实现，`.cmd` 只是入口；两者必须放在同一文件夹。

开发机实测（管理员会话 + 真网关）：
```powershell
powershell -ExecutionPolicy Restricted -File install-gateway-cert.ps1   # → 禁止运行脚本（复现同事）
powershell -ExecutionPolicy Bypass   -File install-gateway-cert.ps1   # → CERT_RESULT=ok
powershell -ExecutionPolicy Restricted -Command "$t=Get-Content -Raw x.ps1; & ([scriptblock]::Create($t)) …"  # → ok
cmd /c install-gateway-cert.cmd <网关地址>                            # → CERT_RESULT=ok，退出码 0
```

## 2026-11-17　S1（出站唯一出口 / outbox v8）上线

**做了什么**：把「机器人说出去的每一句」收进一张表（`outbox` 从「一条消息一行」改成「一句话一行」），
sender 成为唯一出口；新增 `events say`（先答一句）与 `events retract`（说错了撤回），CLI 与 HTTP 网关共用
同一条发布路径。设计 [design-s1-outbox-v8.md](design-s1-outbox-v8.md)，运维 [ops-s1-deploy.md](ops-s1-deploy.md)。

**上线记录**：生产库 `user_version 7 → 8`，243 行一行没少；备份 `state.sqlite3.v7-20261117161116`；
release `0a76dd8e528c235d` → `47993208a08f7e92` → `e4e291dbc446a099`（含下面的修复）；`doctor --assert` rc=0。

**真机验收撞到的 bug（值得记住）**：`say → retract → reply` 的第三步被拒（`completed event has no durable reply`）。
原因是 `retract` 和 `reply` 一样把事件收尾了——`if kind != 'say'` 这个条件把撤回也算了进去，于是「说错了能撤回」
和「干完再报」互相排斥。改成：只有 `reply` 结束事件，`say` 续租，`retract` 是旁路动作（不改 event/inbox，
且事件收尾后也允许撤回已发出的回复）。回归用例 `test_retract_does_not_close_the_event_then_reply_lands`、
`test_retract_after_completed_reply_is_allowed`。

**教训**：本地 1189 条绿测试没抓到它，因为测试是**按设计意图**写的，而设计意图里写的是「retract/reply 都算这段活干完」——
测试忠实地钉住了错误的语义。真机跑一遍才暴露。**能自洽的错，只有真跑才现形。**

**同时清掉的历史积压**：`inbox` 36 条 `needs_attention` → 3 条（剩下的是真需要人看的：2 条读回不一致、1 条回复预算用尽）；
死信事件 46 → 1；清理前另存 `state.sqlite3.pre-prune-20261117164542`（0600）。

## 2026-11-17　S2 开工前的真机数据：批准不能只认「引用回复」

S2（任务卡与审批）的零号动作要求先确认「私聊里的引用回复能不能拿到 `parent_id`」。结论：

- **字段没问题**：生产库里 338 条消息，21 条是引用回复且都带 `parent_id`；其中**私聊里有 3 条**，同样带。
- **配对从未发生过**：DM 里发出过 **68 张**提问卡（`questions.jsonl` 68 条 `ask`），**0 张**被引用回复答复；
  21 条引用回复里**没有一条**引用的是一张提问卡。
- **推论**：字段可得 ≠ 机制可用。人不愿意为了批一张卡去长按引用，所以「只认引用」的批准设计会卡死。
  改动：批准允许两档——引用（精确）或「唯一待批 + 本人私聊 + 明确批准词」（`approval_kind` 记进审计），
  多张待批时必须引用；同时把过期卡片从「保持 open」改成「作废并回群说一句」，免得继续无声堆积。

## 2026-11-17 夜　S2 上线：任务卡与审批，以及两处「说人话」的返工

**上线事实**：库 `v8 → v9`（`tasks` 由 10 列变 18 列，迁移前 `VACUUM INTO` 备份 `state.sqlite3.v8-<时间戳>`，
`integrity ok`、行数不变）；release 依次切过 `d97343e1b3f05a06`（S2 本体，旧卡片样式）→
`071497a0e20eedd9`（卡片改白话）→ `4bad9a37e79c1b6b` / `8b1b8cbd5ef0b6eb`（help 与私聊免 @）→
`b91146b694993f1f`（精确命令表 + 说明书）。`doctor --assert` rc=0，采集恢复 3 秒内。
停机约 3 分钟（比跑本预估的 2 分钟多，因为 `stop` 之后 launchd 条目也一起消失，`install.py` 重新 bootstrap）。

**返工一：卡片先说人话。** 第一版卡片按设计写【活】【为什么是我】【打算怎么做】【会碰到什么】【怎么算做完】，
主人收到后回：「后面你发给我的什么卡片，我都看不懂是什么意思，我不知道要去干嘛」。
诊断是**视角错了**：卡片站在系统视角列举名词，主人要的是"我现在该干嘛"。改成六段人话——
第一行说谁让你干什么、第二行说为什么需要你点头、中间一行「👉 你只要回一个『做』我就开始」
由**代码写死**（模型改不了、截断也剪不掉）、最后说做完你会看到什么；并加禁用词门禁
（账本/审计/链路/running/hints/outbox/approval/state=/schema/迁移/幂等/release/sqlite 出现即退回重写）。
教训：**卡片是交互界面，不是设计文档的摘要**。

**返工二：命令必须精确。** 第二版说明书写「说『停一下』我就停」，主人回：
「说明书语言要精确……上下班的指令就是上班、下班，不要什么停一下、继续」。
于是把「能对桥说的话」收成一张表（`bridge/commands.py`）：`help / 执行 / 状态 / 上班 / 下班 / 清单 / 撤回 / 记住：`
——判定是**确定性字符串匹配，不经模型**；同义词（停一下/继续/挂起/恢复/on/off）不执行，只回一句
「命令要精确」引导语（每个私聊 5 分钟最多一次）。说明书正文（`bridge/help.py`）的命令段由
`commands.table_lines()` 生成，**同一个事实只有一处定义**；另有一条测试断言"每条命令都要有
做什么/什么时候用/示例三行"。

**同一天删掉的两份东西**：`docs/plan-s2-tasks-approval.md`（S2 收工，内容与偏差并入本文）、
`docs/design-b-loop.md` 里已落地的四节（S1 出口唯一、S2 任务卡与审批、审批与授权、`events say/retract`）。
设计稿现在只讲还没做的（S3a 干活端 mac、S3b Windows 桌面版、S4 余项、S5 平台验收），
已完成部分以 `CONTRACTS.md` 为唯一事实。**这一步本身也是 S2 的一部分**：提案落地后不删提案，
下一个人就会照着一份过时的文件做设计。

**S2 与计划的偏差（计划 1 天，实际约 6 人日）**：多出来的是三件事——引用回复原来根本走不到审批
（`details` 的引用分支先于自我会话分流，私聊里的引用卡从未被处理）、`approval_kind` 第一版被塞进
`hints` 元素里（改成独立列）、以及上面两次"说人话"的返工。结论：**判据的语义比判据的代码贵**。

## 2026-11-18　含 @ 的回复从来没有一次通过回读校验

**症状**（生产 `outbox` 里 `kind='reply'` 的终态分布）：

| 回复正文 | 状态 | 条数 |
|---|---|---|
| 不含 `<at ` | `reply_verified` | **238** |
| 含 `<at ` | `reply_verified` | **0** |
| 含 `<at ` | `needs_attention`（`Reply readback mismatch`） | **14** |

不是"偶尔失败"，是**只要回复里 @ 了人，就一次都没通过**。而飞书上这些消息**都发出去了**，
用户看到的是一切正常；只有账本上记着"待人工处理"。

**根因**：`Transport.verify_reply()` 拿回读到的正文和发送时的 body 做逐字相等比较。body 里是
原始标记 `<at user_id="ou_xxx"></at>`，回读时 `_visible()` 把它渲染成 `@张三`——两边永远不等。
校验失败带着 `no resend`，于是这条回复被标成 `needs_attention`，来源消息也一起进了待处理队列。

**为什么一直没人发现**：这个错**自洽得漂亮**——它只把一个状态字段写错，不重发、不报错、
不影响群里任何人看到结果。238 条不含 @ 的回复全过，反而给了"校验在工作"的错觉。真正暴露它的是
把待处理项一条条翻出来看的时候，发现"待处理"里全是同一句话：`Reply readback mismatch`。

**修法**（判据改结构，不是放松成模糊匹配）：

1. `canonical_text()` 把 `<at user_id="…"></at>` 和 `@任意非空白串` 都归一成同一个占位符
   （占位符里不含 `@`，否则第二道替换会替换自己的产物——第一版就这么错过一次）；
2. 正文按归一后比较，**改一个字仍然失败**；
3. 另加一道：body 里 `<at user_id="X">` 的 id 集合必须与回读消息 `mentions` 的 id 集合逐一相等
   （`{'open_id':…}` 与裸字符串两种形态都认），不等就报 `Reply mentions mismatch; no resend`；
4. 失败原因分种类写进 `outbox.error`（正文/提及/发送者/挂错消息/已删除），以后一眼能分开；
5. 历史行不手改库：新增 `feishu-bridge outbox reverify [--dry-run] [--limit N]`，只挑
   `needs_attention` + 那两类错误 + 有 `reply_id` 的行，**用同一份判据重判**，通过才迁移
   （outbox→`reply_verified`、来源 inbox→`completed`），其余原样留着，可重跑、幂等。

**顺带清掉的另一笔债**：`consumers` 表里躺着 `s1-demo` 和四个几个月前的死行。以前只能手改库；
现在有 `feishu-bridge consumer forget <name> [--force]`（最近 10 分钟还登记过的默认拒绝，
打印删掉了什么，只删登记表不动历史事件）。

**教训**：**"校验通过"必须能被一次真实样本证伪。** 逐字比较听起来最严格，实际上它比的是
"飞书把 at 渲染成了名字还是 id"这种与事实无关的东西——越严格的比较，越要先问"它比的是不是
那件该比的事"。另一条：这类错不会自己冒出来，只能靠**定期把 `needs_attention` 全部读一遍**。

### 修完之后还欠一层：待办通知没人关

`outbox reverify` 把 13 行误判修回来之后，账本上仍挂着 **13 条 `attention` 事件**——它们是每次
失败时生成的待办，修好了也没人回头关。于是留下两种僵尸：11 条**来源已经 `completed`、待办还
`pending`**；另有 2 条 `events.state='dead'`，来源也早已处理完。表征是 `doctor` 的
`attention_clear` 永远 `warn`，而"待处理"这个词从此失去意义。

根因不是"忘了清"，是**没有出口**：`needs_attention` 的行和待办事件只能手改库。手改库不算闭环——
下一次还得有个人记得。所以这次补的是路径，不是数据：

1. 修好一行时，它的配套待办**同事务**关掉（`auto-closed: reply verified on reverify`）；
2. `outbox reverify --close-stale` 清历史僵尸，但**只认来源已 `completed` 的**（来源仍有问题的待办不动）；
3. `feishu-bridge attention list|close <ref> --note "…"` 给操作者一条正经出口：`--note` 必填，
   `close` 只改状态、不写 outbox、不改 `reply_id`（关单 ≠ 补一条回复）。

判据：关完之后 `doctor` 的 `attention_clear` 变 `ok`；`tests/test_attention_close.py` 钉住四条
——「修好就关」「没修好不许关」「来源没结案不许清」「关单不伪造回复」。

**待办项的边界（2026-11-18 晚）**：只有「人还能做点什么」才算待办。清理完之后生产上还剩 3 条挂在
`needs_attention`/`dead`：1 条 outbox（`Source message changed/recalled; no stale reply sent`）和 2 条
`dead` 事件（循环预算用尽 / 外部回复超时）——而 `attention list` 当时只列 inbox 与 attention 类，这 3 条
**既看不见也关不掉**。两件事同时补：来源被撤回的回复改成落 `dropped` 终态、不再生成待办；`attention`
扩到三类（inbox / outbox / 任意 dead 事件）。判据是关完之后 `doctor` 的 `attention_clear` 变 `ok`。

---

## 2026-11-18 深夜 测试进程反复 SIGSEGV：一条连接被两个线程同时用

**现象**：`python3 -m pytest -q` 偶发 `Segmentation fault: 11`（rc=139）。不是每次，第二轮、
第七轮、第几轮都可能；单跑任何一个用例都不复现。`~/Library/Logs/DiagnosticReports/` 里的
`Python-*.ips` 从 2 份涨到 9 份，父进程是 `bash`——崩的是**测试进程**，不是常驻服务。

**为什么一直没人发现**：验收命令写的是 `python3 -m pytest -q 2>&1 | tail -1`。管道返回的是
`tail` 的退出码，于是 `rc=139` 被吃成 `0`，"全绿"照常打印。**管道不是证据**：它把被测进程的
成败换成了最后一个命令的成败。这条已经写进 `AGENTS.md` §2。

**两条独立的致命缺陷**（任何一条都足以崩）：

1. `Store` 用一条 `sqlite3.connect(..., check_same_thread=False)` 的单连接。本机 CLT 的
   Python 3.9.6 `sqlite3.threadsafety == 1`——**同一个连接不允许跨线程使用**；`Store` 自己的
   方法有 `self._lock`，但测试里 119 处 `store.connection.execute(...)` 直接绕过锁，与
   `service.details/sender` 的 worker 线程真并发，libsqlite3 的堆被写坏。段错误不是"概率问题"，
   是内存已经被踩了，崩在哪儿全看运气。
2. 测试里 `join(3)/join(5)` 共 20 多处，只有 1 处断言线程真的退出；`addCleanup(store.close)`
   会在泄漏的 daemon 线程仍跑 SQL 时关掉连接。**先关库、线程还在用**，等于把第 1 条的窗口扩大。

**修法（不是加锁，是换连接模型）**：

- `Store.connection` 从属性变成**只读 property**：返回**当前线程**的连接，首次访问时懒创建，
  每条连接自带 `busy_timeout=30000` / `synchronous=FULL` / `foreign_keys=ON`；不再传
  `check_same_thread=False`（默认 True：真跨线程误用会抛 `ProgrammingError`，那是安全网）。
  `store.py` 里 82 处与测试里 119 处 `self.connection`/`store.connection` **一行没改**就变安全了。
- `close()` 关掉**所有**线程的连接，幂等；关掉后访问 `connection` 抛 `RuntimeError('store is closed')`。
- `Service.close(timeout)`：`stop.set()` → 逐个 `join`（总预算，不是每个线程一份）→ **断言
  `is_alive()` 全为 False**（有泄漏就抛 `RuntimeError` 并列出线程名）→ 才关库；`run()` 的
  `finally` 统一走它。
- 测试侧：20 多处 join 后补 `assert not thread.is_alive()`；新增 `tests/conftest.py` 的 autouse
  fixture——用例结束时若还有本次新建的线程活着（等 2 秒仍未退），**直接 fail 并打印线程名**。
- `bridge/memory.py` 的 `Index` 也去掉了 `check_same_thread=False`（它本来就是短命对象）。

**判据（可复跑）**：连续 20 轮 `/usr/bin/python3 -m pytest -q > /tmp/fix-$i.log 2>&1; rc=$?`
退出码全 0；20 轮前后 `Python-*.ips` 数量不变；`grep -rn "check_same_thread=False"` 零命中；
两线程各 100 次读写的临时脚本无异常、连接数为 3（主线程 + 两个 worker）。

**教训**：**"全绿"必须包含退出码，管道里没有绿。** 另有一条更细的：线程泄漏不是测试洁癖——
它是这次事故的引信，所以它现在是一条会 fail 的判据，不是一句"注意一下"。

## 2026-11-18：我把正在跑的那套 dsh 就地升级了，所有会话失效

**做了什么**：`npm i -g @deepseek-ai/dsh@0.2.2-rc.3`。

**当时的环境（关键）**：`dsh web` 正在运行（pid 1640，`npm exec @deepseek-ai/dsh web`），
**当前这个会话就跑在它里面**；GUI 是主人和所有其它会话的入口。

**后果**：替换 `node_modules` 之后宿主进程与磁盘上的代码不一致，所有会话失效、dsh 不可用；
主人用 Cursor Agent 做紧急修复才恢复。修复后的形态是：GUI 由
`~/.dsh/install-packs/dsh-models-20261013/launch-dsh.mjs --bin <dsh lib/bin.js>` 启动
（带 env 覆盖的稳定启动器），运行版本 `0.2.2-rc.4`，监听 `127.0.0.1:8765`。

**为什么是低级错误**：`AGENTS.md` §3 早就写着「重启进程才换代码」，我还亲手在 bridge
上执行过「停服 → 迁库 → 换 release」；结果轮到自己这套工具链，却用了"就地覆盖"。
同一个错误换了个对象，我就没认出来。

**还漏了一步**：动手前没问「要不要先停 GUI」。这一句能挡掉全部损失。

**定下来的规矩**（见 `AGENTS.md` §8）：升级宿主工具链必须「停 → 装 → 起 → 验证」；
验证新版本用隔离安装；动手前先问要不要停 GUI。

**写下来的理由**：这类事故的特点是——**它不出现在我们的测试里**，也不出现在任何
判据里；它只发生在"我以为我在做一件小事"的时候。所以它必须写进规矩，而不是写进记忆。

## 2026-11-18：干活端适配器（PR-2）——没有写第二套 headless 运行器

S3a 需要一个「面向一件活」的执行入口：长跑、可取消、失败必须交出交接包。`dsh-consumer.py`
里已经有一整套：0600 overlay、NDJSON 契约、`start_new_session` + 超时杀进程组、2 MiB 输出上限、
凭证路由。**没有另写一份**，而是搬进 `bridge/workbench.py`，`dsh-consumer.py` 改成调用它，
现有 30 条 consumer 测试全绿作为"行为没变"的判据（overlay 文本 golden 逐字相同）。

新增的是上层：七种结局 `kind`、任务级 1800 秒预算、以及**非 done 必交交接单**
（`handoff.md`/`handoff.json`，九个字段：需求原话、卡在哪一步、做过什么、产物、工作空间、
worktree、可续跑的会话 id 与命令、为什么停）。主人 2026-11-18 的原话是
"杀死以后不能仅仅杀死就完毕，要能接得上"——所以"只杀不交"直接算这个 PR 没做完。

两个踩到的实现坑（都写进测试钉住了）：
1. macOS 上 `/var` 是 `/private/var` 的符号链接，子进程回报的 `cwd` 是真实路径。不统一
   就会把"正常跑完"误判成 `workspace mismatch`（第一次跑测试时 10 条全红，就是这个）。
2. 同一件活重派时，run 目录里已有的 `stdout.ndjson`（O_EXCL 创建）会让重派变成
   `FileExistsError`，直接删掉又会丢掉"上次是怎么死的"证据。改成把上一次现场挪进
   `attempts/<时间戳>/`。

## 2026-11-18：批准之后终于有人干活了（S3a PR-3）

**补的空洞**：S2 上线后批准完任务停在 `running`，账上写着"在干"，其实没有任何进程在干。
PR-3 接上了「批准 → 派活 → 干活端领走 → 跑起来 → 结论落账」。

**三个实现上的坑（都进了测试）**：

1. **`ack_event` 原来只认 `attention`**：`work` 事件领走之后没有"完成"的路，只能干等租约过期。
   修法不是再加一条 `kind='work'` 的分支，而是把合同写清——`attention`/`work` 都没有回复义务，可以 ack；
   `message` 不行，它的终态只能由"真的回了"达成（这条原来是隐含的，现在在 SQL 和 CLI 报错里各写了一遍）。
2. **`CURRENT_LABELS` 漏了新单元**：`_remove_legacy_services` 会把"不在白名单、名字里带 feishu"的 plist
   当成旧命名删掉——第一版干活端单元就是这么被安装器自己删了的。教训：**新增单元必须同时进白名单**，
   否则它只在测试里存在。已验证：安装后 `dsh-worker.plist` 在、`Label` 与参数都对。
3. **自检要用"将要跑的那个"工作台**：`Worker.prepare()` 一开始自己 new 了一个 Workbench，
   跟真正跑的那条工厂路径不是同一个对象——自检过了、真跑挂了。改成走同一个工厂。

**为什么 worktree 必须在这里落地**：干活端是在仓库里改代码的，和主人/别的会话共用一个工作树，
就是 `AGENTS.md` §7 那条"间歇性失败"的原样复现。所以每个活 `git worktree add` 一个独立分支，
主树只读；开不出来就 `blocked` 等人，不退化成"直接在主树里改"。

**判据**：`1436 passed × 3`（rc=0，无管道）、脱敏与出站门禁 8 passed、
`.ips` 9 → 9（SIGSEGV 修复后零新增）、`tests/test_work_events.py` + `tests/test_worker.py` 共 24 条新用例。


## 2026-11-18：S3a PR-4——结果回飞书，以及"被拦住之后接得上"

**这一段补的是最后一跳**：PR-3 之后活真的干了、结论也落账了，但群里/私聊什么都没发生——
主人既看不到结果，也看不到"为什么没干成、怎么接手"。

**三件事定死**（`CONTRACTS.md` 同一条）：

1. **顺序**：入队 → 落账 → ack。反过来一旦崩溃，结论就永远留在账本里、主人永远看不到；
   测试用探针在 `ack_event` 那一刻查 outbox（`test_outbox_is_written_before_the_event_is_acked`）。
2. **落盘兜底**：干完先写 `work/<task_id>/result-a<attempt>.json`，重领时发现它存在就走"补发"，
   **绝不重跑**（重跑会再开一个工作树，结论也可能变）。
3. **接得上**：`blocked` 的私聊问题统一请人回**「执行」**——原设计写的是新造一个「继续」，
   被否掉了：命令词每多一个，说明书、测试、主人要记的东西都要跟着长。而「执行」本来就有
   "推去开工"的意思。`状态` 现在也把卡住的活带编号列出来（`Store.blocked_queue()`）。

**踩到的两处**：

1. **交接单里的系统词把自己挡在了门外**：PR-2 写的"本次用的是 `workspace-write` + `approval=never`"
   正好命中 `BANNED_WORDS`，于是私聊那条**发不出去**，还被 `except` 兜成"结论没能送到飞书"→任务记 `failed`。
   教训：**给机器看的字段名，不能出现在要发给人看的文本里**；`report.speakable()` 现在是外发文案的门禁。
2. **文案一旦散落就会漂**：六种结局的说法、被拦住的问题、长度上限、分条规则，全部收进
   `bridge/report.py` 一处；分条规则直接复用 `help.chunk_text`（说明书与交接单同一套），
   不再各写一份"第 i/n 条"。

**判据**：`1456 passed`（无管道取退出码）、脱敏与出站唯一门禁 8 passed、
`tests/test_report_back.py` 20 条新用例（文案/顺序/幂等/崩溃恢复/重派/上限/`执行` 命令）。

## 2026-11-19：S3a 上线，部署现场抓到两个"装了但没跑"的坑

迁移 v9→v10（`consumers` 加 `role`，`events` 认 `work`）、切 release、起干活端。部署本身顺利，
但**干活端一直起不来**，而且两次都不是同一层原因：

1. **升级路径从来不装干活端**：`install.py --verify` 走的是"已装机器"分支，那个分支只重载、不补写单元。
   S3a 之前的机器压根没有 `io.feishu-agent.dsh-worker.plist`，于是"安装成功、`verdict: ok`"，而
   `launchctl` 里连单元都没有——批准之后没人干活，看起来像活卡住了。修法：升级路径发现单元缺失就补写，
   并且写进测试（`test_upgrade_writes_the_worker_unit_when_it_was_never_installed`）。
2. **入口脚本没有执行位**：release 复制时只给 `-consumer.py` 0700，`dsh-worker.py` 落了 0600。
   launchd 是**直接 exec** 入口脚本（不经过解释器），于是 `last exit code = 78: EX_CONFIG`，
   而 stdout/stderr 两个日志都是空的——最像"什么都没发生"的一种失败。
   修法：`-worker.py` 与 `-consumer.py` 同待遇（0700）；内容没变但权限不对时**只修权限不重写文件**；
   加 `test_release_entry_scripts_are_executable` 钉住。

**教训（又一条"部署才算真验证"的实例）**：这两个坑在 1456 条测试全绿、三次全量绿的情况下都发现不了，
因为测试用的是临时目录、从不真的让 launchd 去 exec 一个新入口脚本。判据得包含**"进程真的起来了"**，
而不是"单元文件写对了"。

3. **测试把真机的 launchd job 顶掉了**：`launchctl` 的 label 是**全局**的，跟 plist 放在哪无关。
   测试与自定义 `--plist` 安装把单元写进临时目录，却照常 reload → 真机上那份 job 被顶成
   "指向一个已被删除的临时路径"，于是 `runs=5`、`last exit code = 78`、两个日志都是空的。
   修法：`_service_dir_is_real()` 闸门——**临时单元目录只写文件、不 reload**；
   两个"reload 语义"用例显式声明自己在验真机目录。
   教训：`launchctl list` 里那个 pid 属于**标签**，不属于你写的那份 plist；判据必须是
   `launchctl print` 里的 `program` 指向哪，而不是"单元文件写对了"。

## 2026-11-19 夜：干活端第一次真跑，抓出三个"看着像凭证问题"的坑

真机端到端（`tsk_db00dddeb901`）跑完 165 秒，最终 `kind=error`、任务 `failed`，交接单里的
"为什么停"写着 `[llm-deepseek-api-key] model listing from … loaded 2 model(s)` —— 一行正常日志。

顺着"凭证路由是不是不一样"查下去，结论是**凭证没问题**：`credential_route()` 在干活端环境里
（launchd 没有 `DEEPSEEK_API_KEY`、`DSH_HOME=~/.dsh`、`~/.dsh/.credentials.yaml` 存在）解析为
`stored`，overlay 末尾那三行 `disabled: false` 一行不少；用同一份 overlay 真跑 `dsh headless`，
7 秒拿到 `{"answer":"收到"}`。**所以最初怀疑的方向是错的**，真问题在别处：

1. **运行现场写错了 home**：`Workbench.home` 默认取 `DSH_HOME`，干活端没传 `work_root`，于是
   stdio 与交接单落进 `~/.dsh/work/<task>/`，而账本 note 指向 `<bridge home>/work/<task>/handoff.md`
   —— 人去看时那里是空的。修法：加 `work_root` 参数，干活端显式传 bridge home。
   **这也是"现场还原不出来"的直接原因**：证据一直在，只是没人想到去 DSH home 里找。
2. **把日志当错误**：失败文案取的是 stderr 的第一行，而 DSH 的 stderr 里混着正常日志。
   修法：先取事件流里的真错误（`status.reason.error.message` / `final.reason.error.message`）
   → 再挑 stderr 里像错误的行（含 error/failed/MISSING_CREDENTIAL/denied…）→ 最后才退回异常文本；
   正常日志行（`model listing from`、`loaded N model(s)`）不参与判定。
3. **干活端坚持要 JSON 答案**：解析器复用了回话端的 `{"answer": …}` 契约，而任务包只要求
   "用三到五行说清做了什么"。模型老实回了人话 → `Invalid DSH answer: agent reply contains no JSON object`
   → 整件活判失败。修法：`RunEvents(parse_answer=…)` 由调用方注入，干活端用 `plain_answer`（纯文本），
   回话端仍走 `answer_of`（JSON）。

**教训**：这三条的共同点是"看起来像配置/凭证问题，实际是**证据与契约的错位**"。
排错时先问一句"我看到的那行字，是程序给的结论，还是它顺手打出来的日志"，
能省掉一整轮方向错误的排查。

## 2026-11-19：干活端第二次真机自检失败——任务书取错了源，而被拦的不是"后台任务"

自检任务（新建一个说明文件并提交）跑了 153 秒后失败，`kind=error`。查下来两个问题，
**第二个还先被我猜错了**：

### 一、模型干的是另一件事（主因）

`events.payload.requirement` 取的是**来源消息原文**，而来源消息是几分钟前一句完全无关的
「把 README 里快速开始那节重写一遍」；模型照着它去读 `bridge/cli.py`、跑全量测试，
压根没碰卡片要求的文件。

修法（v10 → v11）：`tasks` 加一列 `card` 存**卡片正文**，批准派活时
`requirement = tasks.card`（没有卡片的老任务退回 `title`，`card_source` 如实标 `title`），
来源消息另存 `origin_text` **只供排查、不喂模型**；任务包再加三条边界：只做这一件活、
不要顺手改别的文件、不要跑全量测试、不要后台执行。

教训：**"主人批的是那张卡"这句话，之前只写在设计稿里，数据上没落地。** 卡片渲染完就发出去了，
正文没进账本——所以派活时只能退回去用来源消息。一条事实（要干什么）在两处（卡片、来源消息），
系统挑了错的那处。

### 二、被拦的其实不是后台任务，是 `>` 重定向

我先怀疑 `run_in_background: true` 触发策略，**猜错了**。实际用同一个判据函数一测：
`safety.blocked_command('pytest -q > /tmp/x.log')` → `True`——命中的是那条
「单个 `>` 会截断文件，按破坏性命令处理」。`run_in_background` 只是同一句命令里的另一个参数。

定下来的口径：**同一条规则、一个开关**。回话端没有工作树、删了就没了，截断型重定向照旧全禁；
干活端在一次性工作树里跑命令，`cmd > log` 是最普通的写法，放行**只这一条**，
`rm -rf`/`git clean`/`git reset --hard`/`truncate` 照旧拦。
被拦下的结局单独命名 `policy_violation`（不再混进 `error`），交接单里能看出是哪次调用、什么原因。

### 教训

1. **猜机制之前先跑那个判据函数**：一句 `blocked_command(...)` 就能证伪我的第一猜想，成本 10 秒。
2. 错误消息不能取"最后一行输出"：上一轮把一行正常日志当成了失败原因，这一轮把 `>` 规则藏在
   一句含糊的 `policy violation` 后面。**能定位的错误信息**是判据的一部分，不是装饰。

## 2026-11-19：工作树的 `.git` 在主仓库里，沙箱写不进去——提交不能交给模型

第三次真机自检（任务 `tsk_aa3b97ab6727`）又前进了一步：任务书终于对了，模型 45 秒就写出了
`docs/e2e-worker-check.md`，内容也正确。然后它**提交不了**，开始探测环境，撞上策略拦截 → `blocked`。
工作树里留下 `?? docs/e2e-worker-check.md`，没有提交。

模型自己的推理已经说出了根因，原话：*"The git dir is outside workspace. The sandbox blocks writes."*

**机制**：`git worktree add` 出来的工作树，它的 `.git` 是一个指向主仓库 `.git/worktrees/<id>` 的
**文件**。`dsh` 的 `workspace-write` 沙箱只允许写工作树内部，于是 `git commit` 必然被拦。
两个都对的设计，撞在一起就是"文件写出来了、活干完了、却交不出去"。

**定下来的分工**（`CONTRACTS.md`「活的派发与干活端」）：

- 沙箱里的模型只改文件；任务书明确禁止 `git add/commit`，也禁止探测环境（`env`/`printenv`）
- `workbench.run()` 成功后，由 `Worker.commit_worktree()` 用 **worker 自己的进程**（不受沙箱限制）提交：
  只在有改动时提交，信息 `work: <卡片一句话>`，作者沿用仓库配置
- 三种结局分开：有改动→提交并回传短 hash；无改动→不算失败，明说「没有产生改动」；提交失败→`failed` +
  交接单里放 git 原话和一条可粘贴的人工提交命令

**为什么不改成 `git clone`**：clone 一份独立仓库能绕过这个限制，但每个活多一份完整副本（磁盘与
建仓时间都翻倍），而隔离目的已经由 worktree 达到。代价与收益不成比例，所以选择"换谁提交"，
而不是"换隔离方式"。

## 2026-11-19：四分钟没回话，主人以为桥挂了

真机现场：17:26:34 主人在群里发一条要查东西的消息，17:30:43 才收到回复——中间**四分钟群里
一片安静**；紧接着同事那条又等了七分钟（排队 + 真去查）。他后来那句原话是"收不到任何任务卡和响应"。

**事实是它在干活**：那四分钟里 `dsh headless` 一直在跑（翻 LaunchAgents、试 `crontab -l`），
只是全程没说一个字。教训很直接：**在群里，沉默和死亡长得一模一样。**

修法（`consumer_runtime.SlowNotice`，两条都走 outbox，不另开通道）：

* 15 秒还没答完 → 补一句「收到，我先看一下，稍等」；**15 秒内答完则一个字都不多发**——
  快问快答不能被"收到"污染，否则每条回复都拖着一条废话。
* 180 秒还没答完 → 再补一句「还在看，卡在『最近一步』」，**只报一次**，不刷屏。
  "最近一步"取自 NDJSON 的 `tool_call`/`status`（`bridge/workbench.STEP_BOARD`）。
* 回复一进 outbox 就立刻停掉后续提醒：否则人先看到回执、又看到一句迟到的话。
* 线程纪律照旧：提醒线程在 `finally` 里 join；`tests/conftest.py` 的线程泄漏判据会红。

顺带记一条产品事实：这类"只读、查了就能答"的活**不会发任务卡**（起草卡要求"分几步 + 做错代价大"），
所以主人等的是回复、不是卡；卡只在写操作那种场景出现。

## 2026-11-19：主人自己下的写活，回话端直接替他改了仓库——补「免批准但必须走干活端」

主人在群里发：「把 README 里「快速开始」那节按现在的命令重写一遍，改完提交」。
回话端（不是干活端）直接把这件事办了，还在 `ai-gateway` 仓库里提交了 `47ca446`。

**问题不在"他为什么要改 README"** —— 活是干得不错的。问题在三件事全丢了：

1. **没有批准记录**：账本里没有这条任务，事后没法回答"谁批的"；
2. **没有工作树隔离**：改动直接落在主人的工作仓库里；
3. **没有审计**：`executor`、耗时、产物、能否回滚，一条都没有。

根因是起草判据写的是「**群里别人**提的活……代价大才发卡」——本人下的令被当成"他自己做主了"，
于是绕过了整条 S3a 链路。这条判据对别人是对的（别人不能替主人做主），对主人自己就不成立。

**定下来的做法（A 方案）**：本人自己下的**写活**免批准（刚在下令，不必再问），但**必须**走干活端——
独立工作树里干、账本记 `approval_kind='owner_direct'`、`executor`/耗时/产物齐全、干完回群里。
模型在答复 JSON 里用 `direct_work` 对象表达（键与 `task_card` 完全相同，但 `touches` 与
`done_when` 缺一就不派）；别人的活仍然只能走卡片等批准；`task direct` 会校验来源消息的发送者，
app 身份与非本人一律拒绝。

**为什么不是"一律发卡"**：主人下个令还要回来点一次头，是拿他的时间换系统的自我安慰；
为什么不是"维持现状"：那是把权限边界交给了模型的自觉。免批 + 强制走干活端，两头都占。

## 2026-11-19：干活端不许假设 git —— "一个活一个目录"取代"工作树 + 提交"

**主人的话（原样）**：

> mac 和 windows 的 bridge agent 都没有 git，我们是这个系统的开发者，所以才会有 git，
> 安装的同事哪里来的 git 嘛！
>
> 除了我们这台开发机，别人机器上没有 git。**不要有 A 的模式出现。**

**之前错在哪**：S3a 把"开 git worktree → agent 改文件 → 干活端提交 → 回执写 `提交 abc1234`"
当成了通用形态。它只在**我们这台开发机**上成立——我们是这个系统的开发者，机器上才有 git；
同事机上是干净系统，没有 git，也不该替他们装。于是整条链路对同事机根本不成立：
要么第一步 `git worktree add` 就失败，要么逼着我们给别人的机器装 git。

**改成什么**（唯一形态，没有 A/B）：

```
<bridge home>/work/<task_id>/
    space/            ← agent 的工作目录（cwd），产物都写这里
    stdout.ndjson / stderr.log / result-a<n>.json / handoff.md|json
```

- 隔离 = **一个活一个独立目录**（0700），不建工作树、不切分支、不提交
- 产物 = 文件清单（相对 `space/` 的路径 + 字节数），上限 200 条——**超限只截清单，不判失败**，
  结论里写「清单只列前 N 个」
- 回执：`产物：space/report.csv（12.3 KB）`；**任何地方不出现提交号**；没产物就写「跑完了，但没有产出文件」
- `worker` 配置只剩下 `consumer_name` / `max_concurrency`；launchd 单元的参数只剩 `--consumer/--home`
- 代码里**没有**任何 git 调用；`tests/test_worker.py::NoGitDependencyTests` 直接扫源码钉住

**顺带修掉一处真问题**：结论落盘失败（目录被占、磁盘满）以前会把异常抛出去 → 事件被反复重领、
主人什么也收不到。现在落盘失败只在 note 里写「结论没能落盘，崩溃后无法补发」，**结论照发、事件照 ack**。

**教训**：这是同一类错误的第三次——把"我这台开发机上的形态"当成"所有机器的形态"
（前两次是"每个活开 git 工作树"和"干活端提交"）。判断一条设计是不是通用的标准应该是：
**目标机器上有什么？** 而不是"我这台机器上跑得通吗"。

## 2026-11-20：Windows 真机第一次跑 `dsh-worker --check` 就崩了——`prepare()` 里全是 POSIX 假设

**现象**（Windows 开发机，WSL Ubuntu-24.04，`launch=cmd-interop`）：

```
bridge.workbench.WorkbenchError: dsh 起不来：[Errno 8] Exec format error:
  '/mnt/c/Users/…/resources/runtime/cli/bin/dsh.cmd'
```

**根因**：`prepare()` 在 `cmd-interop` 下仍按 POSIX 思路做检查——

1. 版本探针直接 `exec` WSL 视角的 `…/dsh.cmd`；`.cmd` 不是 ELF，Linux 只能报 Exec format error
2. `self.home.is_dir()` / `self.workspace.is_dir()` / `os.access(..., W_OK)` 拿 **Windows 形态**路径去
   stat；`Path('C:\\…')` 在 POSIX 上是 `<cwd>/C:\…` 这种假路径，必然判 False
3. `DSH_HOME` 与配置 home 一个是 POSIX、一个是 Windows 形态，比不出结果

**修法**：`prepare()` 在 interop 下逐项改成「**用 Windows 形态调、用 WSL 形态查**」——

- 版本探针走与生产同一套构造：`cmd.exe /c <Windows 形态 cli> --version`，结果写进 `cli_version`
- home / workspace 先经 `platform_paths.win_to_wsl` 换成 `/mnt/…` 再 stat（`__init__` 里就归一，后续所有判据共用）
- `DSH_HOME` 两边都归一成 Windows 形态再比（尾斜杠等价）；真不一致才报错
- `describe()`（`workbench check`）在两端打印**同样的十行**：新增 `cli_version`、`home_ok`、
  `workspace`、`workspace_ok`，值不同是正常的

**教训**：「同一份代码两套 OS 适配」这句话，最容易漏的正是那些**看起来平台无关的检查**
（stat、可执行位、环境变量比较）。它们平时在 mac 上永远为真，只有在对面那台机器上才暴露。

## 2026-11-20：Windows 真机第二个坑——`cmd /c` 把带空格的路径切开

`dsh-worker --check` 在开发机上输出 `cli_version 未知 FAIL`。手工在 WSL 里跑
`cmd.exe /c 'C:\\Users\\…\\DeepSeek Harness\\…\\dsh.cmd' --version` 是能拿到版本的，
差别就在**参数怎么传**：

```python
# 错的形状（argv 列表，/c 后面跟多个元素）
['cmd.exe', '/c', r'D:\Program Files\DeepSeek\…\dsh.cmd', '--version']
# cmd 会按空格切：`D:\Program` 被当成命令 → 输出为空 → 版本解析成"未知"
```

改成**唯一构造器**：`cmd.exe /c "<cli>" "<arg>" …`（整条命令是 `/c` 的单个参数、路径与每个参数各自加引号，
内层双引号写成两个）。三处调用点（版本探针、headless 运行、`taskkill`）全部走它，不许各拼一次。

**教训（第三次同类）**：Windows 命令行不是 argv——**"列表传参"在 native 程序上成立，在 `cmd /c` 后面不成立**。
凡是要过 shell 的地方，唯一安全的做法是"整条命令一个字符串 + 显式引号"，并且把这条规则收在一个函数里。

## 2026-11-20：Windows 干活端第三个真机坑——`cmd.exe` 不接受 UNC 作当前目录

真机（Windows 开发机，bridge 在 WSL、工作台是桌面版 CLI）跑 `dsh-worker --check`：

```
dsh --version 退出码 1：'\\wsl.localhost\Ubuntu-24.04\home\<用户>\…'
```

原因：干活目录在 **WSL 的 ext4** 上，Windows 看它是 `\\wsl.localhost\…` 的 UNC 路径，
而 `cmd.exe` 拒绝以 UNC 作当前目录——于是连 `--version` 都跑不起来。

修法：
- `worker.work_root` 变成可配置的"每个活的目录根"（默认 `<bridge home>/work`）；
  **Windows 上必须指到 `/mnt/c/…`**（↔ `C:\…`）
- `cmd-interop` 下 cwd 不可见时 `prepare()` **明确报错**并指名改哪个配置，不给"跑起来再说"的机会
- 版本探针在 cwd 不可见时退到 `home` 的上一级（通常是 Windows 用户目录）
- `--check` 多一行 `work_root`（形态 + 是否 Windows 可见），不可见即 FAIL

教训（第三次同类）：**平台假设藏在"路径"两个字里**。前面两次是"git 一定存在""home 是 POSIX"，
这次是"cwd 随便哪都行"。每一条都只有真机才现形。

## 2026-11-20：一次"被真机证伪"的修复——cmd.exe 不要手写引号

**发生了什么**：Windows 开发机上 `dsh-worker --check` 报 `cli_version 未知`。我判断是
`cmd.exe /c` 后面带空格的路径被按空格切开，于是加了一层"整条命令单参数 + 逐项加引号"的构造
（`['cmd.exe','/c','"<cli>" "--version"']`），本地单测全绿（单测只断言"我构造出什么"，断言不了 cmd 怎么解析）。

**真机上的对照实验**（同一台 Windows 开发机、同一 cwd，用 python subprocess 直接比对）：

```
cli  = C:\Users\…\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd
cwd  = /mnt/c/Users/<用户>          # 这次 cwd 是 Windows 可见路径，不受 UNC 干扰

分开传参    ['cmd.exe','/c',cli,'--version']           → rc=0，stdout=['0.2.2-rc.3']   ✅
单参数带引号 ['cmd.exe','/c','"<cli>" "--version"']     → rc=1，
            「'"C:\…\dsh.cmd"' 不是内部或外部命令」   ❌
```

**结论（写进 CONTRACTS，别再犯）**：

1. **WSL → Windows 调 `cmd.exe` 时，argv 各元素分开传，不要自己加引号**——WSL 互操作会按
   Windows 规则给每个元素加引号；手写一层，引号会变成"要执行的程序名"。
2. `cli_version 未知` 的**真因**是 **cwd 是 UNC**（干活目录在 WSL 的 ext4 上 → `\\wsl.localhost\…`，
   cmd 拒绝以 UNC 为当前目录），已由 `worker.work_root` 必须在 `/mnt/<盘符>/…` 上那条判据解决。
3. **单测只能证明"我构造了什么"，证明不了"对方怎么解析"**——凡涉及跨系统命令行，必须有真机对照数据；
   这条和"管道把退出码吃掉"是同一类教训：**判据必须能证伪**。

## 2026-11-20：Windows 干活端第四个真机坑——运行现场写在了 ext4 上

派一件自检活，**15 秒就死**：

```
kind = error
不是 WSL 挂载路径（应以 /mnt/<盘符>/ 开头）：
  '/home/<用户>/.feishu-personal-agent/work/tsk_…/consumer-overlay.yml'
```

真因：`cmd-interop` 下交给 `cmd.exe` 的每个路径都要能映射成 `C:\…`，而**运行现场**
（overlay、stdout、交接单）默认写在 `work_root=self.home`（bridge home，ext4）——
`wsl_to_win` 对 `/home/…` 报错是**正确行为**，错的是"把现场放在 Windows 读不到的地方"。

修法：interop 下运行目录根**必须**是 Windows 可见位置（优先 `space_root`＝`worker.work_root`，
其次 cwd/work_root），`prepare()` 阶段就校验，不可见立刻报错并指名该改哪个配置；
`direct`（mac）布局**逐字不变**。凡是交给 Windows 的路径收敛到 `_windows_target()` 一个出口。

教训（同一条反复出现）：**"我这边的路径"和"对方能读到的路径"是两件事**。前三个坑（exec 的
.cmd、UNC 当 cwd、手写引号）加上这一个，全部只在真机上暴露；单测证明的是"我构造了什么"，
证明不了"对方怎么解析"。

## 2026-11-20（第五个真机坑）：活干完了却被判 workspace mismatch

Windows 开发机（`launch=cmd-interop`）派了一件自检活：`dsh` 真的起来了、`space/report.md` 也写出来了
（108 字节），15 秒后却判失败：

```
kind = error
message = 干到一半出错了：DSH session workspace mismatch
```

`stdout.ndjson` 第一行：

```json
{"type":"session","sessionId":"…","cwd":"C:\\Users\\…\\feishu-work\\tsk_…\\space"}
```

我们对子进程的期望 cwd 是 **WSL 形态** `/mnt/c/Users/…/space`，字符串比较不等 → 误判。

**修法**：interop 下比较前先把两边归一成 Windows 形态（`platform_paths.wsl_to_win`），
并按 Windows 规则比较——盘符与整段 **casefold**、`/` 与 `\` 等价、尾部斜杠去掉；
归一失败（如 child 报了 UNC）算不一致，但错误文案**带上两边原值**（只有一句 mismatch 等于没有信息）。
`direct`（mac）仍是严格字符串比较，一行没动。

**教训**：跨系统比较"同一件事的两种写法"，永远先归一再比；而且**不要拿 `%r` 打日志**——
反斜杠会被转义成 `\\`，人读的时候还得在脑子里解一层。

## 2026-11-20：角色边界落进权限——回话端只说话（read-only），干活端才动手

**背景（两次真机事故）**：回话端 `dsh-main` 自己动手了两次，都没发卡、没派活、账本里零痕迹：

- 11-19：主人让它改 README，它直接在 `ai-gateway` 仓库提交了 `47ca446`
- 11-20（Windows 真机）：主人让它写 `report.md`，它把文件写进了自己的 `~/dsh-workspace`

两次都不是"模型不听话"这么简单：**分工只写在提示词里，权限没拦**。沙箱一直是 `workspace-write`，
所以它想写就能写。这正是本仓那条老规矩的反面——用"自觉"当门禁，迟早失效。

**改法（两道门一起上）**

1. **权限门**：overlay 沙箱按角色分。回话端 `read-only`（读得到、改不了），干活端 `workspace-write`。
   `SANDBOX_MODES` 一处定义，`Workbench(role=...)` 显式传，未知角色直接报错不静默。
2. **提示词门**：回话端 prompt 加「只说话，不动手」段，写清两条正确出口（别人 → 发卡等批；
   主人自己 → `owner_direct` 直派）与**被拦住时的人话**（`report.ROLE_OFFLOAD_LINE`，一处定义）。
   能力模型同步按角色：回话端 `execute=False`。

**为什么不只改一道**：只改提示词 = 靠自觉（已经失效两次）；只改权限 = 模型不知道自己为什么被拒，
会反复重试或对主人说"我做不到"。

**尚未真机验证**：read-only 沙箱下"只读但需要跑命令"的分析（`grep`、`sqlite3` 查询）是否仍可执行，
要在两台机器上各跑一次；若不可行，退到"允许只读命令、禁止一切写入"的中间档。

## 2026-11-20：角色边界的最终划分（主人拍板）

**只读/检索留在回话端，有副作用的走干活端。** 主人的理由原话是「为了回复流畅快速」：
只读操作没有"改坏东西"的风险，审计价值低；真正需要留痕的是**改了什么**。

所以边界不看"这是不是任务"，只看**有没有副作用**：

| 例子 | 谁办 |
|---|---|
| 「README 第一行是什么」「查 SQL 结果」「数 md 文件」 | 回话端（只读，快） |
| 「写文件」「改配置」「提交」「删文件」「导出成文件」 | 干活端（有副作用，进账本 + 独立目录） |

**这条现在由沙箱硬性保证，不靠模型自觉**：回话端沙箱 `read-only`（真机验过：`ls`/`head` 能跑；
`echo > 文件` 得到 `Operation not permitted` + `[sandbox: file access denied]`，模型如实回报失败）；
干活端 `workspace-write`。

设计稿 `docs/design-roles-replier-worker.md` 已并入 CONTRACTS 后删除（按"提案落地即删"）。

## 2026-11-20：安装器补上「一装就有两个消费者」（交付缺陷，不是功能缺失）

Windows 开发机当天的真机验收暴露两件事，都属于**交付层**：

1. **回话端装了但不启动**：`install-dsh-consumer.sh` 只 `enable` 不 `start`。干净机器装完的真实状态是
   "桥在跑、没人回话"——主人的消息停在 `external_queued`，超时后变成
   `No external consumer replied within timeout`，看起来像"桥挂了"。
2. **干活端单元从来没有**：`dsh-worker` 的 systemd 单元是**手写**的（`/etc/systemd/system/…`），
   dist 安装器里根本没有它。手写的东西不算交付。

改法：
- `setup.sh` 新增第 7.5 步：装并启动**两个**消费者（回话端 + 干活端），两个脚本都**默认 start** 并
  **轮询 `consumer status` 直到 `live=true`**（不是"单元 active"就算）；
- 新增 `dist/wsl2/wsl/install-dsh-worker.sh`（系统级单元、先 `--check` 再启动、失败不留死亡循环、
  interop 下自动把 `worker.work_root` 默认写成 `/mnt/<盘符>/Users/<用户>/feishu-work` 并建目录）；
- 第 8 步验收从"三条"改成"六条"：单元都在跑 + **两个消费者都 live** + 干活端 `--check` 通过；
- 新增 `dist/wsl2/wsl/check-consumers.sh`：同一组判据的只读重放版，装完随时可复核。

**教训（与"管道把退出码吃掉"同类）**：判据必须是**"进程真的起来了并且登记了"**，
而不是"单元文件写对了 / 服务看起来在跑"。安装器的"完成"要和用户的"能用"是同一个事实。

## 2026-11-20：as_bot 落地（身份层第二步）——长连接收、回环 ingest、零模式分支

`as_user` 之外增加 `as_bot`：同一套桥、同一套账本，只有"谁在说话"不同。

- **收**：Node sidecar 用 `@larksuiteoapi/node-sdk` 的 WebSocket 长连接订阅 `im.message.receive_v1`，
  把事件 `POST` 到本机 `POST /v1/ingest`。**不做 webhook**——那要求公网 HTTPS 入口，内网机器开不出来。
- **发**：同一个 `lark-cli`，`--as bot`。
- **入队幂等**：`message_id` 为键（`INSERT OR IGNORE`），重投不会重复处理；入队后走**与搜索完全相同**的下游
  （inbox → details → events），有一条用例专门断言"ingest 进来的消息能被 details 领到"。
- **只回环**：`/v1/ingest` 拒绝非 127.0.0.1/::1 来源（403），Bearer 复用网关 token。
- **缺口对账**：`Identity.collect()` 是可选能力（`as_user` 返回 `None`）。机器人没有"@我"的搜索接口，
  只能按窗口读它所在会话再本地筛 @；service 的 collector **只看这个返回值，不看模式**——
  `grep "mode == 'bot'"` 在业务层零命中，这条也写进判据。
- **硬原则没破**：一台机器一种身份；`identity.mode=user` 却带 `identity.bot.app_id` → 启动失败。

教训（延续本仓风格）：**"能力"比"模式"好**。同一件事（补窗口）在两种身份下实现不同，
但业务侧只问"你能对账吗"，问法一样；把差异做成钩子，就没有第二个分支点。

## 2026-11-20：身份层落地（as_user / as_bot），以及三处"装了不启动"的收口

三段工作，一条主线：**功能一样，只是身份实现不同**。

1. **PR-1 纯搬家**：`transport.py` 的实现整体搬进 `bridge/identity/user.py`，
   `transport.py` 只留 re-export 壳（`def/class` 数为 0）。判据是"既有 1597 条测试一行不改、全绿"。
2. **PR-2 as_bot**：`BotIdentity` + Node sidecar 长连接 + 回环 `POST /v1/ingest`；
   断线缺口用搜索对账（`collect`）补。业务层仍然零 `mode == 'bot'` 分支。
3. **PR-3 安装/命令通道/doctor**：装什么由 `identity.mode` 决定；`owner_open_ids` 是 approver 名单；
   bot 的命令通道是"你与机器人的私聊"；`doctor` 增 `identity_sidecar` 硬判据。

两条教训（都是同一个病）：

- **"装了不启动"永远看起来像成功**：WSL 上回话端只 `enable` 不 `start`，桥上一切正常但没人回话；
  bot 的 sidecar 同理（`identity_channel=ws` 但单元没起来）。判据必须是"进程真的在跑/真的领过事件"，
  不是"单元文件写了""配置里有这一项"。
- **身份是"谁在说话"，不是"多一个开关"**：所以 `owner_open_ids` 只能出现在 bot 模式；
  user 模式写它是错的（本人只有 `target_open_id`），两条用例把边界钉住。

一台机器一种身份是**硬原则**：配置矛盾（user + bot.app_id）启动失败；user 模式残留 sidecar 时
`doctor` 与 WSL 安装器都 fail——不做"按群路由"这种半并存，因为那必然双答。

## 2026-11-21：as_bot 真机三个坑（open_id 按应用隔离 / 长连接会被别的客户端抢 / 命令通道是私聊）

**一、飞书 open_id 是"按应用隔离"的。** 同一个人在不同应用下有不同 open_id：
本人在 `cli_aaa504…` 下是 `ou_c746…`，在 `cli_aa883a4d…` 下是 `ou_076c…`。
于是 as_bot 的"主人判定"必须用**当前应用下的 open_id**，用别的应用的 id 会永远判不出主人，
表现为"命令不生效、只有普通对话"。开发机首测的 `help` 不返回说明书就是这个原因。

**二、长连接（WebSocket）一个应用只能被一个客户端占用。** 本机这台的应用被 mac 上的
`hermes gateway`（launchd `ai.hermes.gateway`）先接走了，所以 Windows 开发机的 sidecar
虽然 `ws client ready`，但 `im.message.receive_v1` 一条都收不到。停掉 hermes 之后立刻通了。
判据：sidecar 日志出现 `forwarded om_…` 才算真收到事件；`ws client ready` 不算。

**三、as_bot 的命令通道是"与机器人的私聊"，不是"自聊群"。** as_user 用 `self_chat_ids` 白名单群；
as_bot 下机器人不可能自己给自己发消息，所以命令只认 p2p 私聊（`identity.command_channel=dm`），
且仍要求发送者在 `owner_open_ids` 里。群里发命令两种模式都不执行。

顺带记两条判据教训：
- 比对"回复是否等于说明书"必须把**切条**拼回来再比（1385 字 > 1200 → 会切成两条，末条带「（完）」）；
  只比最后一条会得出"内容不一样"的错误结论。
- `doctor` 的 `authenticated` 判据目前仍按"用户身份"写，as_bot 下会误报 fail —— 待补。

## 2026-11-21：as_bot 收尾——"手工补的两步"变成安装器自动做

真机上 as_bot 能跑，但有两件事是**手工补的**，换台机器照文档装就会踩；本轮把它们收进安装器与体检：

1. **ingest token 与回环网关**：sidecar 启动第一件事就是读 `<home>/gateway-token`，再把事件
   `POST 127.0.0.1:<port>/v1/ingest`。当时两样都没有：token ENOENT、且 sidecar 默认 8787 与
   `events serve` 默认 8766 不一致。现在 `install.py` 在 bot 模式自动生成 token（幂等、不轮换）、
   写并启动 `io.feishu-agent.ingest-gateway`、等 `/health` 通；user 模式停用并删除它。
2. **`doctor` 的 `authenticated` 判据**：它按"用户 token"写，as_bot 下永久误报 fail。
   现在判据由 `bridge/identity/factory.py::doctor_checks()` 按模式给：as_user 判用户 token；
   as_bot 判 bot 身份 + app_id 一致 + token 0600 + 网关 `/health`。

顺带守住了那条纪律：**`bridge/cli.py` 里没有 `mode == 'bot'`**（`NoModeBranchingTests` 会红）——
模式差异全部收在身份层，业务层只拼体检行。

判据：`tests/test_identity_ingest_gateway.py`（16 条：token 权限/复用不轮换、单元写入与清理、
端口非法即报错、bot 安装写端口、体检六种形态）；`dist/wsl2/setup.sh` 的 as_bot 验收多了
"网关 active + token 0600"两条，user 模式多了"网关必须退场"一条。

## 2026-11-21：as_bot 收尾——把"手工补的两步"变成安装器自动做

真机验收 as_bot 时发现两个"换台机器就会踩"的缺口，都不是功能缺失，而是**交付路径没走完**：

1. **ingest 通道要手工备**：sidecar 启动即读 `<home>/gateway-token`，缺文件直接报
   `cannot read ingest token file … ENOENT`；而且 sidecar 默认送 `127.0.0.1:8787`，
   而 `events serve` 默认 `8766`，端口还对不上。现在安装器（bot 模式）自动生成 token（0600、幂等不轮换）、
   写并启动 `io.feishu-agent.ingest-gateway`（回环 8787）、等 `/health` 通、把端口写进配置；
   `user` 模式则停用并删除该单元（幂等）。`doctor` 增加 `ingest_token` / `ingest_gateway` 两行硬判据。
2. **`authenticated` 判据按"用户身份"写**：as_bot 下永久 fail（机器人没有用户 token）。
   现在判据下沉到身份层：`as_user` 查用户 token、`as_bot` 查"bot 就绪 + app_id 一致"，
   业务层仍然零 `mode == 'bot'` 分支。

真机复核（Windows 开发机，删掉手工产物后重装）：
`install rc=0`；token 自动生成（0600）；网关与 sidecar 都 active；
`doctor --assert` rc=0，`authenticated` / `ingest_token` / `ingest_gateway` 三行 ok。

教训与之前"装了不启动"同类：**判据必须落在"东西真的在工作"上**（token 能读、`/health` 200、
身份真的 ready），而不是"文件写出来了"。
