# 给「Windows + DSH」同事的部署流程（形态 A：WSL2 全内）

> **单一事实源分工**：本文件是**交接流程**（角色、顺序、P0–P7 卡点、环境依赖）；命令与退出码的权威版在
> [dist/wsl2/README.md](../dist/wsl2/README.md)，差异/手工路径在 [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md)。
> 三份重复的验收断言只维护那一份。

> **定位**：从**裸机**到**能收发回复**的完整执行手册——所有环境依赖（Windows/WSL2/Ubuntu/systemd、
> Node、lark-cli、飞书自建应用、bubblewrap、DSH CLI、Tailscale、OpenSSH）都写了安装、配置、启动与验证。
> 拓扑定义只看 [WINDOWS-DECISION.md](../WINDOWS-DECISION.md)；发行包结构与阶段细节看 [dist/wsl2/README.md](../dist/wsl2/README.md)；
> 手工路线/术语表/退化路径看 [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md)；远程通道看 [docs/ops-remote-manage.md](ops-remote-manage.md)；
> **一次给多台同事机部署（≥3 台）**看 [docs/windows-fleet-rollout.md](windows-fleet-rollout.md)（批次、只读探测、台账、只读批量复核）。
>
> **适用**：同事只有这一台 Windows，飞书收发 + DSH 回复都在本机跑（形态 A）。
> bridge 留在你的 Mac、Windows 只做消费者 → 形态 B（§11），那是另一条流程。
>
> **真机基线**（2026-11-02 跑通的一台，供对照）：Windows 11 24H2 Home（build 26100.7462）、
> WSL2 Ubuntu 24.04.5（kernel `6.18.54.1-microsoft-standard-WSL2`）、Linux 用户 `pc`、
> 系统级 systemd unit + Windows 计划任务 holder，Mac 侧全程经 Tailscale + Windows OpenSSH 远程执行。
> 标 ⚠ 的条目按「装新机要复核」对待：2026-11-02/03 已在一台真实 Windows 11 上端到端跑通（见 [docs/HISTORY.md](HISTORY.md)），但企业管控（MDM、AppLocker、组策略）会改变行为。

---

## 0. 环境依赖总表（先对表，再动手）

| # | 组件 | 必须？ | 装在哪 | 谁做 | 验证命令 | 没装的后果 |
|---|---|---|---|---|---|---|
| 1 | Windows 10 2004+（build ≥19041）/ 11；BIOS 已开 VT-x/AMD-V；能拿管理员 | **必须** | Windows | 同事 | `[Environment]::OSVersion.Version.Build`；任务管理器→性能→虚拟化 | 装不了 WSL2 |
| 2 | WSL2 + VirtualMachinePlatform | **必须** | Windows | 同事（管理员） | `wsl --status`、`wsl -l -v`（VERSION=2） | exit 2 / 3010 |
| 3 | Ubuntu 发行版 + **普通** UNIX 用户（不要 root） | **必须** | WSL2 | 同事 | `wsl -d Ubuntu -- bash -lc 'id -u'`（非 0） | exit 2 |
| 4 | systemd：**发行版** `/etc/wsl.conf` 的 `[boot] systemd=true` | **必须** | WSL2 | 安装器 A 阶段/同事 | `wsl -d Ubuntu -- bash -lc 'ps -p 1 -o comm='`→`systemd` | exit 3；`setup.sh` 首步退出 |
| 5 | apt 基础包 `python3 sqlite3 curl ca-certificates git npm` | **必须** | WSL2 | Prereq C 阶段 | `python3 -V; sqlite3 --version; git --version` | Prereq C 阶段 exit 6 |
| 6 | Node（lark-cli 需 ≥18；**DSH 需 ≥20.12**） | **必须** | WSL2 | Prereq C 阶段（18）/ 同事升级（22） | `node -v` | lark-cli 装不上 / DSH 在 `parseEnv` 崩 |
| 7 | lark-cli + **飞书企业自建应用** + 5 个 scope + 发布审批 | **必须** | WSL2 + 飞书开放平台 | 同事 | `lark-cli auth status --json`；`auth check --scope …` | `authenticated=false`、exit 5/7 |
| 8 | 仓库代码 + `install.py` 运行时 | **必须** | WSL2 ext4 | 安装器 B/C 阶段 | `feishu-bridge status` | — |
| 9 | systemd **系统级** unit + 限定 sudoers | **必须** | WSL2 | 安装器 D 阶段（Install 模式） | `bash dist/wsl2/wsl/verify.sh` | WSL 断开即停 |
| 10 | Windows 保活：计划任务 `FeishuBridgeBoot` + holder | **必须** | Windows | 安装器 E 阶段（Install 模式） | `schtasks /query /tn FeishuBridgeBoot` | 发行版被回收、服务静默停 |
| 11 | DSH CLI + 原生平台包（`node-addon-*`） | **必须（回复）** | WSL2 | 同事 | `dsh --version`；`dsh headless --help` | 消费者起不来，没人 claim 事件（桥自己不会回答） |
| 12 | 沙箱后端：`bubblewrap`（Linux 首选）或可用 Landlock | **必须（DSH）** | WSL2 | 同事 | `bwrap --version` | `SANDBOX_UNAVAILABLE`，命令拒绝执行 |
| 13 | DSH 凭证（API key 或 `~/.dsh/.credentials.yaml`） | **必须（回复）** | WSL2 | 同事 | consumer `active` 且 `events status` 的 `last_seen_at` 在跳 | `MISSING_CREDENTIAL`，消费者不领事件 |
| 14 | DSH 常驻消费者 unit `io.feishu-agent.dsh-consumer.service` | **必须（回复）** | WSL2 | 脚本 | `systemctl is-active …`；`events status` 心跳 | 没人 claim 事件 |
| 15 | Tailscale（**同事机用 auth key 入你的 tailnet，不需要他的账号**，见 §8.0） | 推荐* | Windows + Mac | 你签 key / 同事跑一条命令 | `tailscale status`；`tailscale ping <对端>` | 无法远程协助；形态 B 无法成立 |
| 16 | Windows OpenSSH Server + 你的公钥 + Tailscale-only 防火墙 | 推荐* | Windows | Prereq D 阶段（装 sshd）/ 你（公钥） | `ssh -o BatchMode=yes <user>@<host> 'echo ok'`、`Get-Service sshd` | 只能坐到机器前 |
| 17 | Mac 侧 `tailscale` / `ssh` / 本仓 `tools/*` | 推荐* | Mac | 你 | `tools/bridge-remote.sh … status` | 同上 |

\* 推荐 = 不影响本机收消息，但决定「以后能不能远程修」。**P1 能跑起来就立刻做 P7**，后面几步你就能远程看。

> **第 16 项的边界**：`prereq.cmd` / `-Mode Prereq` 的 D 步**默认就装** Windows OpenSSH Server
> （防火墙默认只放行 Tailscale 网段，并停用系统自带的「全来源」规则；`-AllowLanSsh` 才额外放行局域网；
> 公司策略不许开 sshd 时用 `-SkipOpenSsh`）。但**装了 sshd ≠ 能远程登录**：公钥授权、`administrators_authorized_keys`
> 权限与加固仍是手工步骤，归 §8.3。

### 0.1 形态 A 全景（含远程通道）

```text
                  ┌──── Tailscale tailnet（你的账号；同事机用 key 入网） ────┐
Mac（你）─────────┤  MagicDNS: <你的Mac>.<tailnet>.ts.net               │
tailscale / ssh   │  对端:     <同事机名>.<tailnet>.ts.net              │
                  └───────────────────────┬─────────────────────────────┘
                                          │ SSH 22（仅 100.64.0.0/10 + fd7a:115c:a1e0::/48）
                                          ▼
Windows（同事，保持登录）
  OpenSSH Server（sshd，Automatic，公钥登录，密码关闭）
  计划任务 FeishuBridgeBoot（登录触发，隐藏窗口）
    └─ feishu-holder.ps1（常驻、进程内自愈）
         └─ wsl.exe -d Ubuntu --exec /bin/sleep infinity   ← 顶住发行版不被回收
WSL2 Ubuntu（systemd，ext4 家目录）
  ├─ /etc/systemd/system/io.feishu-agent.bridge.service (User=<同事>, Restart=always)
  │    └─ ~/.feishu-personal-agent/bin/feishu-bridge foreground
  │         ├─ collector（12s 轮询群聊 @）→ SQLite → details → message 事件（等消费者 claim）
  │         └─ sender → 飞书 reply（唯一出口；确定性 reply_uuid / 日配额 / 幂等 / outbox）
  └─ /etc/systemd/system/io.feishu-agent.dsh-consumer.service
       └─ python3 current/dsh-consumer.py（release 里的副本；claim → dsh headless（bwrap/landlock）→ reply/ack/fail）
```

### 0.2 阶段门禁（每阶段过了再进下一步）

| 门 | 阶段 | 一句话通过标准 |
|---|---|---|
| **M0** | P0 裸机 + 飞书应用 | build ≥19041、虚拟化开、管理员可用；飞书应用已建、5 scope 已开、已发布审批 |
| **M1** | P1 环境就绪（`prereq.cmd`，或 P1 首次安装） | `ps -p 1`=`systemd`、`wsl -l -v`=v2、用户非 root、`lark-cli --version` 有输出、`Get-Service sshd`=Running（未加 `-SkipOpenSsh` 时） |
| **M2** | P2 飞书身份 | `auth status` openId 稳定；`auth check` 5 条全 `ok:true` |
| **M3** | P3 安装收尾 | `install.cmd` 退出码 0、桌面报告生成 |
| **M4** | P4 健康 | `check.cmd`：authenticated、worker 全活（`WORKER_NAMES`）、systemd scope=system、sending=true |
| **M5** | P5 DSH | 消费者 `active`、`last_seen_at` 在跳、`doctor --assert` 的 `consumer_offline` 为 ok |
| **M6** | P6 端到端 | 群里 @ 一次 → 只回一条，日志 `reply sent` / `reply verified` 齐全 |
| **M7** | P7 远程 | 你从 Mac `ssh` 能进、`bridge-remote.sh check` 退出码 0 |

---

## 1. P0：Windows 裸机 + 飞书开放平台应用（两条线并行）

### 1.1 Windows 自检（同事，管理员 PowerShell）

```powershell
[Environment]::OSVersion.Version                       # Build 必须 ≥ 19041
Get-ComputerInfo | Select-Object WindowsProductName, OsBuildNumber, OsVersion
systeminfo | findstr /i "Hyper-V"                      # 中文系统看“虚拟化: 已启用”
Get-CimInstance Win32_Processor | Select-Object Name, VirtualizationFirmwareEnabled
Get-PSDrive C | Select-Object Used, Free               # 至少留 5 GB
```

- 虚拟化没开 → 进 BIOS 打开 `Intel VT-x` / `AMD-V(SVM)`。
- 先确认**能拿到管理员**（公司 MDM 常锁）；装 WSL / 计划任务 / SSH 都要它。
- 网络要能到：apt 源、npm registry、`open.feishu.cn`；装 DSH 消费者还要能取到 DSH 的平台包（Cursor 消费者另需 `cursor.com`）。
- 安全软件/组策略不能禁 WSL2、`wsl.exe`、计划任务；被禁就没有绕法，只能走形态 B。
- 时间误差会让 OAuth token 直接失败：`w32tm /resync` 一次不亏。

### 1.2 飞书企业自建应用（同事，控制台操作；**审批可能几小时，先启动**）

> 这是整条链路**最容易卡住**的一步。`lark-cli` 首次使用必须先绑定应用；安装器的登录窗口只跑
> `lark-cli auth login`，**不代做应用配置**，所以应用必须先在开放平台准备好。

1. <https://open.feishu.cn/app> → **创建企业自建应用** → 记 `App ID`（`cli_...`）与 `App Secret`。
   飞书中国版品牌 `feishu`；Lark 国际版 `lark`（选错全 4xx）。
2. **权限管理** → 添加 6 个 scope：前 5 个是 `bridge/transport.py` 强校验的（少一条就
   `authenticated=false`）；第 6 个 `im:message.p2p_msg:get_as_user` 是**私聊正文**权限，
   缺了私聊通道会默认关着（服务日志一条 warning，`teach status` 会说明缺哪条）：

   ```text
   im:chat:read
   im:message
   im:message.group_msg:get_as_user
   im:message.p2p_msg:get_as_user
   im:message.send_as_user
   search:message
   ```

3. **版本管理与发布** → 创建版本 → 申请发布 → **企业管理员审批**（部分租户自动通过）。
4. **可用范围**加入同事本人（否则拿不到用户 token）。
5. bridge 是**主动轮询**（`im/v1/messages/search` + `chat-list` + `reply`）：**不需要**事件订阅/回调/长连接，
   **不需要**机器人能力，不要配 webhook。

> 两条路：同事自己建应用（推荐，密钥不外传）；或复用公司里同一个已审批的应用（要能拿到 secret，
> 且应用可用范围含他本人）。**不要**把 Mac 的 `~/.lark-cli/config.json` / `~/.feishu-personal-agent/` 拷给他——那是你的 token 和状态库。

### 1.3 交接物清单（你 → 同事）

- `dist/windows/feishu-bridge-windows-<日期>.zip`（打包：`bash dist/wsl2/pack-windows.sh`，见 [dist/wsl2/README.md](../dist/wsl2/README.md) §十；约 600 KB，飞书/网盘发）。
- （离线场景）`dist/windows/win-ssh-bootstrap-v1.1.zip` + `tools/windows/ssh-bootstrap/harden-tailscale.ps1`：不开外网也能装 SSH，见 §8.3 方案 0 与 [NOTES.md](../tools/windows/ssh-bootstrap/NOTES.md)。
- 你的 Mac 公钥 `~/.ssh/id_ed25519.pub` 全文（P7 远程通道用，可选）。
- 署名文案（回复末尾，例如「来自小张的AI助手」）。
- 他需要自己准备的：飞书账号、DeepSeek API key 或 DSH 登录；Tailscale **客户端**（用你签的 auth key 入网，
  **不需要注册 Tailscale 账号**，见 §8.0）。

> **M0**：Windows 三条命令输出正常、虚拟化已开、管理员可用；飞书应用已建、5 scope 已开、已发布并审批通过。

---

## 2. P1：首次 `install.cmd`（WSL2 + Ubuntu + systemd + 代码 + 依赖）

### 2.1 它会做到哪一步

`install.cmd` → `install-windows.ps1` 六阶段，**首次**会依次：

```text
A 环境   Windows build/WSL2 检查 → 装 Ubuntu → 建 Linux 普通用户 → 写发行版 /etc/wsl.conf [boot] systemd=true
        → wsl --shutdown → 验证 systemd（失败 exit 3）
B 代码   把包里的 repo\ 镜像进 WSL2 ext4 家目录 ~/feishu-personal-agent（不能放 /mnt/c；源树删掉的文件会一并移除）
C 安装   ① 问回复署名 → ② 装 apt 依赖 + Node + lark-cli（setup.sh --deps-only，首次 3–5 分钟）
        → ③ 打开「飞书登录」窗口 → ④ 拿 open_id → ⑤ setup.sh（install.py → 验收）
D 托管   系统级 unit + 停用户级 + scoped sudoers → 启动
E 保活   写 holder 脚本 + 注册计划任务 FeishuBridgeBoot → 立刻拉起
F 验收   verify.sh 逐条断言 → 桌面报告 feishu-bridge-install-<时间>.md
```

先跑 **`prereq.cmd`**（= `-Mode Prereq`）也可以，而且**推荐给新机器**：它只做前置资源
A 环境 → B 代码 → C 依赖（`setup.sh --deps-only`）→ D Windows OpenSSH → E 体检，**不装 bridge**、
不登录飞书、不写 `~/.feishu-personal-agent`。好处是失败时分得清「环境没装好」还是「bridge 没配好」。
顺序与幂等判定只在 [dist/wsl2/README.md](../dist/wsl2/README.md) §3.3.1 定义，通俗说明见
[docs/windows-prereq-install.md](windows-prereq-install.md)；前置就绪后再跑 `install.cmd`。

> 目标机**没有外网**（或你不想让它依赖 apt/npm/商店）：用离线安装包，三步 `00-自检 → 01-装前置环境 → 02-验收`，
> 载荷（Ubuntu 镜像 / WSL MSI / Node / lark-cli / sqlite3 deb / OpenSSH MSI）随包，生成与撤销见 [dist/wsl2/README.md](../dist/wsl2/README.md) §3.4。

### 2.2 执行

```powershell
# 解压 zip 到不带中文/空格的目录，然后（管理员 PowerShell；install.cmd 也会自己弹 UAC）
.\install.cmd
# 已知 open_id + 署名时：.\install.cmd -TargetOpenId ou_xxx -Signature "来自小张的AI助手"
```

**两个停顿点**（`-TargetOpenId` 只在「已登录」时才用）：

1. **Ubuntu 首次初始化**：自动弹 Ubuntu 窗口 → 按提示建**普通用户**（不要 root）→ 回安装窗口按 Enter。
2. **飞书登录**：弹 WSL 窗口跑 `lark-cli auth login`。**先别急着按 Enter**——应用还没绑定，它会立刻失败。
   打开**第二个** Ubuntu 窗口按 §3 完成 `config init` + `auth login`，再回安装窗口按 Enter，安装器会重新读身份并**继续**。

### 2.3 如果不小心按了 Enter（拿不到 open_id）

安装器以**退出码 5**结束（`拿不到飞书 open_id`）。这不是装坏了：WSL 环境、代码、依赖已经就绪。
按 §3 在 WSL 里补完绑定与登录，然后**原样重跑** `install.cmd`（幂等：A 阶段检测后跳过、B 阶段重新同步代码、
C 阶段继续装；`~/.feishu-personal-agent/` 里的配置与状态不会被覆盖）。

### 2.4 手工 / 无发行包路径（调试或不能传 zip 时）

```powershell
# Windows 管理员：只装前置资源和环境（A 环境 → B 代码 → C 依赖 → D OpenSSH → E 体检，不装 bridge）
.\repo\dist\wsl2\install-windows.ps1 -Mode Prereq
```

下面是**完全不用安装器**时的手工等价步骤（`-Mode Prereq` 只代做了前两块：代码落位与 `--deps-only`；
bridge 安装与托管仍需手工做，或改跑 `install.cmd`）：

```bash
# WSL2：把 repo 复制进 ext4 家目录（不能放 /mnt/c）
# 注意 rm -rf 只清代码树，不动数据目录 ~/.feishu-personal-agent/
rm -rf ~/feishu-personal-agent && cp -r /mnt/c/<解压路径>/repo ~/feishu-personal-agent
cd ~/feishu-personal-agent/dist/wsl2
bash setup.sh --deps-only          # apt/node/lark-cli（不含登录，不含 install.py）
# → 按 §3 完成 config init + auth login，然后：
TARGET_OPEN_ID=ou_xxx bash setup.sh        # 完整安装 + fail-fast 验收
sudo bash wsl/finalize-systemd.sh           # D 阶段：系统级 unit + scoped sudoers
```

```powershell
# 再跑一次（仍然推荐）：补齐 E（holder + 计划任务）与 F（验收报告），幂等
# 包内直接跑可省略 -SourcePath（脚本自动定位上层 repo\）；从 clone 的仓库跑时才指定
.\repo\dist\wsl2\install-windows.ps1
```

> 纯手工 `schtasks` 建保活任务在旧版本会每 5 分钟闪一次窗口（`0064f84` 回归）；让安装器做 E 阶段最稳。

### 2.5 失败对照（环境段）

> 下表只列**现象**，不是退出码表；码的定义与完整列表只看 [dist/wsl2/README.md](../dist/wsl2/README.md) §3.2。

| 现象 | 原因 | 处理 |
|---|---|---|
| 退出码 **3010** /「WSL 尚未初始化」 | 刚装完 WSL 需要重启 | 重启 Windows 后原样重跑 |
| 退出码 **2**（含发行版名不符） | 没建普通用户 / 发行版显示名不是 `Ubuntu` | 打开 Ubuntu 完成初始化；`wsl -l -q` 看真名，用 `-Distro` 指定 |
| 退出码 **3** / 只有 `systemd` 没有 `USER_SYSTEMD_OK` | `/etc/wsl.conf` 没写进 / 没 `wsl --shutdown` / 老内核 | 重做：`wsl -d Ubuntu -u root -- bash -lc 'printf "[boot]\nsystemd=true\n" | tee /etc/wsl.conf'` → `wsl --shutdown`；必要时 `wsl --update` |
| `wsl -l -v` 显示 VERSION=1 | 老发行版没转 v2 | `wsl --set-version Ubuntu 2`（BIOS 虚拟化必须已开） |
| Store 不可用导致 `wsl --install -d Ubuntu` 失败 | 企业镜像无 Store | `wsl --install --web-download`，或从 Store 手工装 Ubuntu 后 `wsl --import` |
| `WSL 里还没有 lark-cli ... sudo 需要密码` | 安装器依赖 `sudo -n` | 在弹出的 WSL 窗口按提示输密码；远程 SSH 场景先手工 `sudo -v` 一次 |
| `install.cmd` 闪退/被杀软拦 | 计划任务或 wsl.exe 被策略禁用 | `Get-MpPreference` / 组策略确认；被禁只能走形态 B |
| `Prereq` 的 D（OpenSSH）/ E（体检）不过 | sshd 可选功能被策略禁用 / 某项前置断言失败 | 见 [docs/windows-prereq-install.md](windows-prereq-install.md) §五；策略禁用加 `-SkipOpenSsh` |

> **M1**：`wsl -l -v`（VERSION=2）、`ps -p 1 -o comm=`=`systemd`、`USER_SYSTEMD_OK`、`id -u`≠0、`lark-cli --version` 有输出、`Get-Service sshd`=Running（未加 `-SkipOpenSsh` 时）。

---

## 3. P2：lark-cli 绑定应用 + 登录（在 WSL2 里）

> 时机：安装器停在「飞书登录」窗口时，**另开一个 Ubuntu 窗口**做本节；或安装器已以 exit 5 结束后做，
> 再重跑 `install.cmd`。在**普通 WSL 终端**里执行（DSH/agent 会话里 `config init` 默认拒建应用，
> 要 `--force-init` 或改用 `lark-cli config bind`）。

```bash
# ① 绑定应用（A：已有 app_id/secret，推荐，secret 走 stdin 不进 ps）
printf '%s' '<App Secret>' | lark-cli config init --app-id cli_xxx --brand feishu --app-secret-stdin
#    B：没有应用时让 CLI 打开浏览器带你创建/绑定
#    lark-cli config init --new

lark-cli config show        # appId / brand 正确
lark-cli whoami             # identity / onBehalfOf.userName 是本人

# ② 登录 + 最小 scope（WSL 没浏览器：把打印的 URL 复制到 Windows 浏览器完成；
#    不写 --scope 会申请所有业务域权限——bridge 只需要这 5 个，最小权限即可）
lark-cli auth login --scope "im:chat:read im:message im:message.group_msg:get_as_user im:message.p2p_msg:get_as_user im:message.send_as_user search:message"
# 无 TTY/agent 环境卡住时：--no-wait --json 拿 device_code → 用户授权 → --device-code <code>

# ③ 验证
lark-cli auth status --json     # identities.user.openId=ou_...、available=true、tokenStatus=valid
lark-cli auth check --scope "im:chat:read im:message im:message.group_msg:get_as_user im:message.p2p_msg:get_as_user im:message.send_as_user search:message" --json
#  期望 {"ok":true,"missing":null}

# ④ 记下 open_id（bridge 的 target_open_id 必须与它完全一致，每 60s 校验一次）
lark-cli auth status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["identities"]["user"]["openId"])'
```

- scope 少了 / 审批还没过 → `auth check` 报 `missing`。回 P0-1.2 补权限 → **重新发版审批** → 再 `auth login`（增量授权，
  多次登录 scope 累积，不会撤掉旧的）。
- 登录完成后回安装窗口按 Enter（或重跑 `install.cmd`），安装器会取到 open_id 继续 C/D/E/F。

> **M2**：`auth status` openId 稳定；`auth check` 5 条全 ok。

---

## 4. P3：安装器收尾（C 后半 → D → E → F）

```text
C 后续   setup.sh：apt 依赖（再次确认）→ Node/lark-cli
         → install.py（写 ~/.feishu-personal-agent、切 release）→ fail-fast 验收
D 托管   finalize-systemd.sh：系统级 unit + 停用户级 + scoped sudoers → 启动
E 保活   holder 脚本 → 计划任务 FeishuBridgeBoot（登录触发 + 隐藏）→ 立刻确认 holder 在位
F 验收   verify.sh 逐条断言 + 桌面报告
```

退出码 → 下一步：**唯一权威表在 [dist/wsl2/README.md](../dist/wsl2/README.md) §3.2**（四种模式、每个码的含义与下一步）。
本文不再重复该表：同一事实写两处，将来必有一处是错的。

装完的形态（别按 macOS 直觉理解）：

- 代码 `~/feishu-personal-agent`（WSL2 **ext4**）；数据 `~/.feishu-personal-agent/`（`config.json`、`status.json`、
  `releases/`、`bridge.log`、`bridge.stdout.log`/`bridge.stderr.log`）。
- 服务：`/etc/systemd/system/io.feishu-agent.bridge.service`（**系统级**，`User=<同事>`，`Restart=always`）；
  用户级同名 unit 被停用（避免抢同一 home）；`feishu-bridge start|stop|restart` 走 scoped sudoers 免密。
- 保活：计划任务 `FeishuBridgeBoot`（登录触发、隐藏）→ `%LOCALAPPDATA%\feishu-bridge\feishu-holder*.ps1`
  → 隐藏的 `wsl.exe --exec /bin/sleep infinity`。
- 桌面级常驻：Windows 关机/注销后停，登录后计划任务拉起（真机实测 35 秒内恢复）。
- 不监听公网端口；8766/8768 网关**不在**发行包里。

> **M3**：退出码 0 + 桌面报告；`feishu-bridge status` 能输出 JSON。

---

## 5. P4：验收 bridge + 打开发送开关

```powershell
# Windows 普通 PowerShell（只读，不需要管理员）
.\check.cmd
```

| 断言 | 期望 |
|---|---|
| `process_running` | `true` |
| `receiving_healthy` | `true`（`mode=held` 时有意为 false） |
| `authenticated` | `true`（token + 5 scope 都行） |
| `workers` | collector / details / sender 全 `true` |
| `systemd.scope` | `system` |
| `running_release` vs `installed_release` | 一致（`update_pending=false`） |
| `sending_enabled` | **`true`** ← 新装默认 `false`，收件正常但**一条都不回** |

```bash
# WSL2（新装必做；feishu-bridge 在 ~/.local/bin，登录 shell 的 PATH 里）
feishu-bridge resume
bash ~/feishu-personal-agent/dist/wsl2/wsl/verify.sh
feishu-bridge doctor --assert        # 退出码 0 = 通过
```

> **M4**：上表全绿 + `sending_enabled=true`。

---

## 6. P5：DSH 回复引擎（Node 升级 → CLI → 沙箱 → 凭证 → 常驻消费者 → 开闸）

### 6.1 升级 Node 到 22（DSH 硬要求；安装器装的是 18）

```bash
node -v                                  # 18.x → 必须升；22.x 跳过本节
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
node -v                                  # v22.x
# 若报 nodejs/npm 冲突（安装器装过 apt 的 npm）：sudo apt-get remove -y npm nodejs 后重试
```

### 6.2 装 DSH CLI + 平台原生包

```bash
sudo npm install -g --prefix /usr/local @deepseek-ai/dsh
dsh --version                            # 作者环境 0.2.1-rc.2
dsh headless --help >/dev/null && echo DSH_HEADLESS_OK
```

平台包缺失（`No usable native binding found for node-addon-require-builtin-linux-x64-gnu` 或
`Cannot find module '@deepseek-ai/node-addon-system-linux-x64/package.json'`）：

```bash
sudo npm install -g --prefix /usr/local --include=optional @deepseek-ai/dsh@<版本>
```

### 6.3 沙箱后端 bubblewrap（Linux 首选 bwrap，其次 Landlock）

```bash
sudo apt-get install -y bubblewrap
bwrap --version
```

没有可用后端时 DSH 会以 `SANDBOX_UNAVAILABLE` **拒绝执行**（绝不无沙箱运行），消费者会起不来。

### 6.4 放凭证（二选一）

```bash
# A) API key 写进 0600 的 env 文件（无头场景最直接，不经过 argv）
sudo bash -c 'printf "DEEPSEEK_API_KEY=%s\n" "sk-<同事自己的 key>" > /etc/feishu-bridge/dsh.env'
sudo chmod 0600 /etc/feishu-bridge/dsh.env

# B) 在 WSL2 里登录一次 DSH（生成 ~/.dsh/.credentials.yaml）
sudo -u "$USER" -H dsh
```

- 消费者的 `DSH_HOME` 默认 `~/.dsh`（WSL 侧）。Windows 桌面 DSH 的 `C:\Users\<他>\.dsh` 是**另一套路径**，
  不保证自动同步；两边内容一致（sha256 相同）才能直接用，否则走 A 或 B。
- `dsh headless` **默认禁用**已保存登录，裸跑报 `MISSING_CREDENTIAL` 是**假报错**；消费者靠 `dsh-consumer.py` 的
  overlay（`credentials`/`authorization`/`deepseek-account` 三个插件 `disabled:false`）恢复登录；
  以 `events status` 里 `consumers[].last_seen_at` 是否在跳为准。

### 6.5 装成系统级常驻 unit

消费者跑的是 **release 里的副本**（`~/.feishu-personal-agent/current/dsh-consumer.py`），
与 macOS 一致：`releases/<id>/` 是唯一可运行副本，仓库树只用来装/升级。
因此**升级发行包之后要重启这个单元**才吃到新代码（安装器的 D 阶段会自动重载；手工路径见下）。

```bash
# 只体检（不写系统、不启动）；会打印 unit 的 ExecStart 和 current 指向哪个 release
sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/install-dsh-consumer.sh --user "$USER" --check
# 关注 DSH_CONSUMER dsh_headless= / credential= / node_major= / unit_execstart= / current_release=

sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/install-dsh-consumer.sh --user "$USER" --start
systemctl is-active io.feishu-agent.dsh-consumer

# 换了 release（升级过包/切过版本）之后：
sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/install-dsh-consumer.sh --user "$USER" --restart
```

### 6.6 起来就在领（没有"开闸"这步）

消费者是这台机器唯一的大脑：单元 `active` 就开始 claim `message` 事件，回复由 bridge 的 sender 发出。
"没回复"只有两种原因：消费者没在领（`consumer_offline`），或消息本来不该回（`ignored`）。

```bash
systemctl is-active io.feishu-agent.dsh-consumer
feishu-bridge events status          # consumers[].last_seen_at 应在跳
feishu-bridge doctor --assert        # consumer_offline 应为 ok
# 临时不回复：feishu-bridge hold -r "下班"（收件也一起停）；恢复用 feishu-bridge release

# 可选：一条命令做"真实凭证探针 + 重启消费者"（装完/换凭证后跑一次最省事）
sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/enable-reply.sh --user "$USER"
#   --check   只探不重启     --disable 停消费者（消息继续排队，不再回复）
```

### 6.7 失败对照

| 现象 | 处理 |
|---|---|
| `credential_verified missing` / `MISSING_CREDENTIAL` | 检查 `/etc/feishu-bridge/dsh.env`（含 key、0600）；或 `sudo -u $USER -H dsh` 登录一次 |
| `SANDBOX_UNAVAILABLE` / probe 失败 | `sudo apt-get install -y bubblewrap`；确认内核 ≥5.13（Landlock） |
| 原生绑定缺失 | §6.2 的 `--include=optional` 补装 |
| consumer `active` 但 `last_seen_at` 不动 | `journalctl -u io.feishu-agent.dsh-consumer -n 50`；再看 `feishu-bridge events status` |
| 没凭证就把消费者起来 | 消费者会在 claim 前失败，事件照样排队；先凭证，再看 `last_seen_at` |

> **M5**：consumer `active`、`last_seen_at` 秒级跳、`doctor --assert` 里 `consumer_offline` 为 ok。

---

## 7. P6：端到端验收（只看群聊 @）

采集固定为 `is_at_me=true + chat_type=group`：**私聊 @ 不采集**；「跟自己的自聊」只用于「下班/上班」控制通道。

1. 让同事在**任意群**里 @ 他一次（或拉个小群 @ 自己）。
2. `feishu-bridge logs` 预期两行：`reply sent` → `reply verified`，且**只回一条**。
3. `feishu-bridge status` 的 outbox 走到 `reply_verified`。

不通过按命中率查：`sending_enabled` → 消费者有没有在领事件（`events status` 的 `consumers[].last_seen_at`、`doctor` 的 `consumer_offline`）→ 消费者心跳 →
`logs -n 100` 的 sender 报错 → `doctor --assert` 哪项 fail。

> **M6**：群里 @ 一次 → 1 条回复 → 日志 `reply sent` / `reply verified` 齐全。

---

## 8. P7：Tailscale + OpenSSH（远程协助通道；建议 P1 之后就做）

### 8.0 入网方式：auth key（同事**不需要**注册 Tailscale 账号）

Tailscale 没有「用户名 + 密码」可注册，身份只能来自 IdP（Google / Microsoft / GitHub 等）。所以正确做法不是
「给每个同事开一个 Tailscale 账号」，而是**你签一张 auth key，那台机器带着 key 入你的 tailnet**：同事不登录、
不注册、不需要任何账号，机器照常出现在你的设备列表里，你能 SSH 进去就完事。

| 谁 | 做什么 | 成本 |
|---|---|---|
| 你 | 控制台签一张 key | 30 秒/台 |
| 同事 | 设一个环境变量，跑一条命令，把报告发回来 | 5 分钟/台 |
| 你 | `ssh` 进去验证（§8.5） | 10 秒/台 |

#### 8.0.1 你这边：签一张 key

控制台 `https://login.tailscale.com/admin/settings/keys` → **Generate auth key**：

- **Reusable**：不勾。一次性、一台一张；可复用 key 等于把「往你网络里加机器」的权限长期放出去。
- **Expiration**：`1 day`。当天发、当天用、过期自灭。
- **Pre-approved**：**勾上**。tailnet 若开了 device approval，不勾会卡在「待批准」，而你看不到同事的屏幕。
- **Tags**：单台机器**先不勾**。勾了就是 tagged device：归属从你的用户变成 tag、节点密钥默认不过期
  （普通设备会到期，见 §8.0.4）、不占用户席位；代价是必须同时改 ACL（`tagOwners` + 只允许你的设备访问 22）。
  **多台/多人、或要「同事之间互不可见」时再做这层**；单台先用最简形态——加一层自己看不懂的策略比不加更危险。
- 生成后复制 `tskey-auth-...`。它是「能往你 tailnet 里加一台机器」的凭据：只私发给那一台的同事，
  泄了就在 Keys 页 **Revoke**（已入网的机器不会掉，只是这张 key 作废）。

> **别让同事登你的 Tailscale 账号**（本仓旧版文档是这么写的，已废弃）：那是把账号密码/2FA 交出去，
> 而且设备归属变成「你的身份」，事后分不清哪台是谁的、也没法只收回一台。
> 免费 Personal 套餐现在是**最多 6 个用户**；auth key 加进来的是「设备」不是「用户」，几台机器根本不用考虑邀请人。

#### 8.0.2 你这边：把下面这段话发给同事（**这条命令在全仓只定义这一次**）

```text
麻烦你花 5 分钟把远程通道装好，之后我远程帮你装，你不用自己敲部署命令。

1) 先确认 Windows「设置 → 系统 → 系统信息」里"虚拟化"是已启用。
   管理员 PowerShell 跑：  wsl --install -d Ubuntu
   装完重启一次；重启后会自动弹 Ubuntu 窗口，按提示建用户名和密码（别跳过、别关窗口）。
2) 解压我发的 feishu-bridge-windows-<日期>.zip，管理员 PowerShell 进到解压出来的目录，跑这两行：

   $env:TS_AUTHKEY = 'tskey-auth-<我单独发你的>'
   .\repo\tools\windows\setup-remote-debug.ps1 -MacPubKey "ssh-ed25519 <我的公钥>"

3) 窗口最后会打印一段"报告"（含 SUMMARY / tailscale / sshd 几行），整段复制发我。

任何一步报错就停，把退出码和红字发我；不要自己改脚本、不要改防火墙。
```

key 走进聊天记录也不要紧（一次性 + 1 天过期 + 只够加一台机器），但**不要**把可复用的 key 发出去。

#### 8.0.3 同事那条命令做了什么、回执怎么读

`setup-remote-debug.ps1` 依次做：装/启动 Tailscale → 用 `$env:TS_AUTHKEY` 入网（不弹浏览器；脚本自提权时会把
key 以 `-AuthKey` 转发给提权子进程，所以 key 不出现在同事敲的那行命令里）→
装 OpenSSH Server → 生成主机密钥 + 最小 `sshd_config`（`PasswordAuthentication no`）→
只对 `100.64.0.0/10` + `fd7a:115c:a1e0::/48` 建入站规则、并按「端口 22 + 来源 Any」**停用**系统自带的
全来源规则（停不掉就失败退出，不会留一个假的安全状态）→ 写入 Mac 公钥（管理员两个位置都写）→
自检 22 是否 listen → 打印并保存报告。

> sshd 这一段的实现在 `tools/windows/install-openssh.ps1`，`setup-remote-debug.ps1` 只是**调用**它
> （两处曾各有一份，收敛策略已经各走各的：这里只告警、那边停用 → 已删掉本脚本那份）。
> 因此分发时 `tools/windows/` 要整目录一起给（发行包的 `repo/` 本来就带）。

回执只看三处：`SUMMARY ... verdict=OK`；`tailscale` 段 `BackendState=Running` 且有 `100.x`；
`sshd` 段 `service=Running` + `listen22=True`。退出码含义见 §8.3 方案 A。

#### 8.0.4 节点密钥到期：180 天，查与续（**auth key 过期 ≠ 设备过期**）

入网用的那张一次性 auth key 只在「把机器加进 tailnet 那一刻」有用；之后这台机器能连多久，
只看它自己的**节点密钥**：普通设备（入网时不勾 Tags）默认 **180 天**到期，管理台设备页显示 `Key expiry: in N days`。

到期后设备自动掉出 tailnet：`ssh` / `tools/remote_doctor.py` / `tools/bridge-remote.sh` 全部连不上，
而这台机器上没有 Tailscale 账号（§8.0），同事自己无法重新登录，只能由你处理。
**所以这是唯一一条「不管就会自己坏掉」的链路**。

**查**（在管理机 Mac 上跑；每台的绝对到期日以它为准，别用「装机器那天 + 180」去推）：

```bash
python3 tools/windows_host.py --all     # 一行一台：称呼 / 机名 / 在线 / key expiry（[live] 取自本机 tailscale）
python3 tools/windows_host.py <名字>    # 单台卡片，到期日带剩余天数
```

查询实现只有 `tools/windows_host.py` 一处（它把 `KeyExpiry` 解析成「DISABLED / 绝对时间 + 剩余天数」），
不要在文档里再抄一份 `tailscale status --json | python3 -c ...`：那既是第二份实现，又会踩 Python 3.9
只认 3/6 位小数（Tailscale 是 9 位纳秒）的坑。

**续**（到期前做；一旦过期，只剩「坐到机器前」这一条路，远程手段全部失效）：

1. 按 §8.0.1 新签一张一次性 key（`1 day` + Pre-approved，单台不勾 Reusable/Tags）。
2. 从 Mac 进那台机器（先在**到期前**验证 SSH 还能连，别在最后一天做）：

```bash
ssh <winuser>@<台账里的 magicdns>
```

3. 会话里是提权管理员 PowerShell（Windows OpenSSH 给管理员账户的是 High integrity token），直接：

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' up --auth-key=tskey-auth-<新签的>
& 'C:\Program Files\Tailscale\tailscale.exe' status   # 回 Mac 再跑一次上面的查，确认到期日已推到 ~180 天后
```

**不想维护这张到期表**：管理台 `Machines` → 选中该设备 → `⋯` → **Disable key expiry**，逐台一次、永久生效；
不用换 key、不用碰那台机器、同事什么都不用做。**多台部署（≥3 台）一开始就该这么处理**，
或者按 §8.0.1 走 tagged device（tag 设备的节点密钥默认不过期，还能顺带限制「同事之间互不可见」）。
Tailscale 会在到期前约 30 天给管理账号发提醒邮件——那是兜底，不是计划。

### 8.1 前提

- tailnet 是**你的**：具体名字与节点清单**不在文档里抄**——查台账（`python3 tools/windows_host.py --all`，
  `magicdns` 列给出 `<机名>.<tailnet>.ts.net`）。同事机用 auth key 入网（§8.0），不需要他的账号，也不需要共享密码。
- Windows 原生**不提供** Tailscale SSH server → 必须装 **Windows OpenSSH Server**（§8.0 的脚本已装）。
- 22 端口**只对 Tailscale 网段**开放，**不要**暴露公网/局域网。

### 8.2 手工/排障：单独装 Tailscale（§8.0 的脚本已包含这一步，正常不用做）

```powershell
# A) winget（有 winget 最省事）
winget install --id Tailscale.Tailscale --exact --silent --accept-package-agreements --accept-source-agreements

# B) 没有 winget：官方 MSI
#    https://tailscale.com/download/windows
#    或 https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi

$ts = 'C:\Program Files\Tailscale\tailscale.exe'
& $ts up --hostname "<机名>-win" --auth-key=tskey-auth-xxx   # 推荐：§8.0.1 签的一次性 key
# 没有 key 才退回浏览器登录 —— 那等于让同事登你的账号，只在你人就在机器前时用：
# & $ts up --hostname "<机名>-win"
& $ts status                            # BackendState=Running；Self 行有 100.x
& $ts status --json | ConvertFrom-Json | % { $_.Self.DNSName }   # MagicDNS 名
```

- 记下 MagicDNS 名（形如 `<机名>.<tailnet>.ts.net`，真值抄 `tools/windows_host.py` 的输出），后面 `ssh` / `remote_doctor` 都用它。
- 想注销后仍能远程进：Tailscale 托盘 → Settings → 勾 **Run unattended**。
  但 bridge 的 holder 是**登录触发**的，常态应保持同事处于登录状态。
- 若组织改了 tailnet ACL，需要管理员允许 Mac → Windows 的 22 端口（没动过 ACL 就是默认 allow all）。

### 8.3 Windows：安装并配置 OpenSSH Server

#### 方案 0：离线一键包（无外网 / 无 winget / 批量装机时首选）

包名 `win-ssh-bootstrap-v1.1.zip`（本机固定副本 `dist/windows/win-ssh-bootstrap-v1.1.zip`，
文本归档与校验值在 [tools/windows/ssh-bootstrap/NOTES.md](../tools/windows/ssh-bootstrap/NOTES.md)）。
自带微软官方 `OpenSSH-Win64-v10.4.0.0.msi`，不依赖网络；解压后**右键 `install.bat` → 以管理员身份运行**，
除了 UAC 没有别的输入。跑完抄下窗口末尾那行 `SUMMARY host=… sshd=Running listen22=True verdict=OK`。

```powershell
# 装完必做：把 22 从「Any 全开 + 密码登录」收紧到 tailnet + 仅公钥
# 这个脚本不在 zip 里（避免改动已验证的包），从本仓 tools/windows/ssh-bootstrap/ 拷到同一目录再执行
.\harden-tailscale.ps1 -DisablePassword        # 需要管理员；会留 sshd_config.bak-<时间戳>
```

与方案 A 的差异（务必知道）：离线包的防火墙规则是 **`Profile Any`、不限来源**，且**不关密码登录**；
登录账户必须是本机 Administrators 组成员（Windows OpenSSH 只对管理员读 `administrators_authorized_keys`）；
`keys.txt` 里每把私钥 ≈ 本机管理员口令，`configure.ps1` 只增不减（收回权限要手工删行）。
它的 MSI 是 x64，ARM64 机器不适用。

#### 方案 A（有外网时）：一键脚本

**命令与 §8.0.2 完全一样**（`$env:TS_AUTHKEY` + `setup-remote-debug.ps1 -MacPubKey ...`），这里只列它额外支持的可选参数：

```text
-HostName <设备名>   -AuthKey <tskey>   -SshPort 22   -AllowLan
-SkipTailscale   -SkipSshd   -ProbeWslBridge（只读探测 WSL 里的 feishu-bridge）   -NoPause
```

脚本会：装/登录 Tailscale → 装 OpenSSH Server（`Add-WindowsCapability` 失败自动退回 GitHub Win32-OpenSSH MSI）→
生成主机密钥 + 最小 `sshd_config`（`PasswordAuthentication no` + 管理员 `administrators_authorized_keys`）→
注册并启动 `sshd`（Automatic）→ 建 `OpenSSH-Server-Tailscale-In-TCP` 防火墙规则（仅 `100.64.0.0/10` + `fd7a:115c:a1e0::/48`）→
写公钥（管理员账号 `C:\ProgramData\ssh\administrators_authorized_keys`，普通账号 `~\.ssh\authorized_keys`，两处都写）→
自检 22 是否 listen → 打印并保存报告（把报告整段发回给你）。

退出码：`0` 全就绪；`1` 未提权/UAC 取消；`2` Tailscale 未就绪；`3` OpenSSH 未就绪；`4` 公钥写入失败；`5` 端口没 listen。
只装 SSH 不动 Tailscale：`.\repo\tools\windows\install-openssh.ps1`（`-Port` / `-AllowLan` / `-NoPause`）。

#### 方案 B：手工逐条（需要逐行审计时）

```powershell
# ① 安装 OpenSSH Server（家庭版/WSUS 失败时用方案 A 的 MSI 兜底）
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# ② 启动 + 开机自启
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Get-Service sshd

# ③ 主机密钥 + 最小 sshd_config（只许公钥）
& "$env:SystemRoot\System32\OpenSSH\ssh-keygen.exe" -A
$cfg = "$env:ProgramData\ssh\sshd_config"
@(
  'Port 22'
  'PubkeyAuthentication yes'
  'PasswordAuthentication no'
  'AuthorizedKeysFile .ssh/authorized_keys'
  'Match Group administrators'
  '       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys'
) -join "`r`n" | Set-Content -Path $cfg -Encoding ASCII
Restart-Service sshd

# ④ 写公钥（必须 ASCII/无 BOM；管理员账号走 administrators_authorized_keys，ACL 只留 Administrators + SYSTEM）
$authKeys = "$env:ProgramData\ssh\administrators_authorized_keys"
Set-Content -Path $authKeys -Value "<你的 id_ed25519.pub 全文>" -Encoding ASCII
icacls $authKeys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F"

# ⑤ 防火墙：只放行 Tailscale 网段，并禁用系统自带的全网段规则
New-NetFirewallRule -Name 'OpenSSH-Server-Tailscale-In-TCP' -DisplayName 'OpenSSH Server (Tailscale only)' `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 `
  -RemoteAddress @('100.64.0.0/10','fd7a:115c:a1e0::/48') -Profile Any
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP','OpenSSH SSH Server Preview (sshd)' -ErrorAction SilentlyContinue |
  Disable-NetFirewallRule

# ⑥ 自检
& "$env:SystemRoot\System32\OpenSSH\sshd.exe" -T | Select-String 'passwordauthentication|pubkeyauthentication|port'
Get-NetTCPConnection -LocalPort 22 -State Listen
```

手工最易漏两处：④ 的 ACL（漏了 sshd 拒绝公钥，日志在 `C:\ProgramData\ssh\logs\sshd.log`）与 ⑤ 的默认规则禁用
（漏了等于对局域网开放 22）。

### 8.4 WSL 侧还要装 Tailscale 吗？

**不用**。WSL2 走 Windows 网络栈（NAT），Windows 进 tailnet 就够；从 Mac 进 WSL 的路径是
`ssh <win user>@<MagicDNS>` → 再 `wsl.exe -d Ubuntu -- bash -s`。

### 8.5 从 Mac 验证

```bash
tailscale status | grep -i <机名>
tailscale ping <台账里的 magicdns>

ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new <winuser>@<台账里的 magicdns> 'echo SSH_OK'
ssh -o BatchMode=yes <winuser>@<台账里的 magicdns> 'wsl.exe -d Ubuntu -- uname -a'

tools/windows/check-remote-debug.sh --host <台账里的 magicdns> --user <winuser>
python3 tools/remote_doctor.py --host <台账里的 magicdns> --target windows \
  --markdown reviews/remote-doctor-$(date +%F).md
python3 tools/remote_doctor.py --host <台账里的 magicdns> --target wsl

export BRIDGE_WIN=<台账里的 magicdns> BRIDGE_USER=<winuser>
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" status
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" doctor --assert
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" check      # doctor + WSL verify.sh
```

### 8.6 远程操作纪律（真机踩过）

1. **连续 ssh 会被 reset**（`kex_exchange_identification: ... reset by peer`）：两次间隔 ≥3s，失败后 sleep 10s 重试。
2. 从 stdin 跑 `powershell -NoProfile -Command -` 是**逐行执行**，多行 `if/foreach` 会被截断 → 用 `-EncodedCommand`
   或落地 `.ps1` 用 `-File`。
3. Windows `cmd.exe` 会回显整条命令行，输出头部噪音不是报错。
4. 进 WSL 一律 `wsl.exe -d Ubuntu -- bash -s`（正文走 stdin）；不要 `sh -c`（会落在 cmd.exe 上）。
5. 22 端口规则不要改成 `Any`；临时互通走 Tailscale，不要开公网。

> **M7**：`ssh ... 'echo SSH_OK'` 成功、`Get-Service sshd`=Running/Automatic、`check-remote-debug.sh` 无 fail、
> `bridge-remote.sh check` 退出码 0。

---

## 9. 日常运维（转给同事）

```bash
# —— bridge（WSL2）——
feishu-bridge status                  # 健康快照
feishu-bridge logs                    # 最近 80 行；-f 跟随；--all 连后端 stderr
feishu-bridge restart
feishu-bridge hold -r "下班"           # 挂起：零采集/零入队/零回复，持久化
feishu-bridge release                 # 恢复：只处理此刻之后的新消息
feishu-bridge self-chat --add <oc_...>   # 之后可在自聊里发「下班 / 上班」
feishu-bridge bots --disable          # 别的机器人 @ 时不互刷（reply_to_bots=false）

# —— DSH 消费者（WSL2）——
systemctl is-active io.feishu-agent.dsh-consumer
sudo journalctl -u io.feishu-agent.dsh-consumer -n 50 --no-pager
feishu-bridge events status           # consumers[].last_seen_at / 事件积压
feishu-bridge doctor --assert          # consumer_offline 必须 ok（没人领事件就是硬失败）
```

- **升级**：拿新 zip 重跑 `install.cmd`（幂等；C 阶段自动走 `wsl/upgrade.sh`）。
  这台机器没有 `.git`，**没有** `git pull` 这条路；远程升级走 `tools/bridge-remote.sh … upgrade <zip>`
  （回执与设备上的 `installation.json` 对 release id，别只看"命令返回 0"）。
- **回滚**：先看 `docs/ops-remote-manage.md` §4 —— 2.0 起 upgrade 会把 `state.sqlite3` 就地迁移到新 schema
  且不可逆，**必须先退库再退代码**，只重发上一版 zip 会撞上旧代码打不开新库（`RuntimeError` → 重启循环）。
  macOS 侧才用 `feishu-bridge rollback --to <id>` + `restart`。
- **备份**：`feishu-bridge stop` 后打包 `~/.feishu-personal-agent/`（config/state/releases），再 `start`；别只备份源码。
- **卸载（保留数据）**：`.\repo\dist\wsl2\install-windows.ps1 -Mode Uninstall`。
- **彻底清理**：`schtasks /delete /tn FeishuBridgeBoot /f` + `wsl --unregister Ubuntu` + 删 `%LOCALAPPDATA%\feishu-bridge`。
- **重启 Windows 后自检**：登录 → 等 1 分钟 → `feishu-bridge status`、`schtasks /query /tn FeishuBridgeBoot`。

---

## 10. 硬红线（照抄给同事）

1. 失败**先报告**，不要改脚本/自建中转/换架构绕过——会把「已知缺陷」变成「没人见过的新故障」。
2. **原生 Windows 不装 bridge**（POSIX 缺失）；仓库/数据放 ext4，**不要放 `/mnt/c`**；不要跨机共享 `state.sqlite3`。
3. Windows 侧**不要直接调 lark-cli 发消息**：唯一出口是 bridge 的 sender 线程（reply_uuid/日配额/幂等都在里面）。
4. 消费者只 claim 事件、只提交 reply/ack/fail；正文只走 stdin 或文件，**不放命令行参数**。
5. 不要声称形态 A/B 沙箱等价：WSL2 常驻入口有 bwrap/landlock + 流式兜底 + 出站脱敏；原生 Windows 会话没有。
6. **不要**把 Mac 的 `~/.feishu-personal-agent/`、`~/.lark-cli/`（token/密钥）拷给同事；飞书应用密钥走安全渠道。
7. 没凭证时别急着把消费者起来（它会在 claim 前失败，事件照旧排队），也不要**在没人领事件时把它当"装好了"**：`doctor --assert` 的 `consumer_offline` 会立刻报出来。不要把 22 / 8766 / 8768 暴露到公网或局域网。

---

## 11. 什么情况下走形态 B（另一条流程）

只有：公司不允在这台机器开 WSL2；或 bridge 已常驻在 Mac，Windows 只是再加一个消费者。
此时 Windows **只做消费者**：对端 `feishu-bridge events token` + `events serve` → Windows 用
`ssh -N -L 8766:127.0.0.1:8766 <mac>.tailnet.ts.net` 建隧道 → 把 `skills/feishu-bridge-consumer-remote`
整目录复制到 `%USERPROFILE%\.dsh\skills\`（DSH 扫 `.dsh\skills` 和 `.agents\skills`，**不扫** `.codex`）→
会话 `/loop 30s` 跑 `SESSION-PROMPT-WINDOWS.txt`。详见
[skills/feishu-bridge-consumer-remote/SKILL.md](../skills/feishu-bridge-consumer-remote/SKILL.md)。

---

## 12. 验收记录（让同事回传）

```text
P0  Windows 版本/构建=           虚拟化=           管理员=是/否
    飞书 app_id=cli_   brand=   5 scope=已开/未开    发布审批=已过/待审
P1  wsl -l -v：发行版/版本=      ps -p 1=          USER_SYSTEMD_OK=        Linux 用户/uid=
    install.cmd 首次结果=  退出码=   （0 / 5=登录未完成）
P2  auth openId=ou_             auth check={ok,missing}=
P3  install.cmd 最终退出码=       桌面报告=有/无        VERSION.txt package/repo_head=
P4  process_running/receiving_healthy/authenticated/workers/systemd.scope/sending_enabled=
P5  node -v=    dsh --version=    bwrap --version=    credential=ok/    consumer=active/inactive    consumer_offline=ok/fail
P6  群聊 @ 于 <时间>，回复条数=     日志：reply sent / reply verified =
P7  tailscale 设备名=        ssh SSH_OK=        check-remote-debug=        bridge-remote check 退出码=
重启 Windows 后自检：feishu-bridge status=        holder/计划任务=
```

---

## 附 A：按执行环境速查

| 环境 | 主要命令 |
|---|---|
| **Windows 管理员 PowerShell** | `wsl --install -d Ubuntu` / `wsl --update` / `wsl --shutdown` / `wsl --set-version` / `prereq.cmd` / `install.cmd` / `install-windows.ps1 -Mode {Install,Prereq,Check,Uninstall}` / `setup-remote-debug.ps1` / `install-openssh.ps1` / `schtasks` / `Add-WindowsCapability` |
| **Windows 普通 PowerShell** | `check.cmd` / `Get-Service sshd` / `tailscale status` / `install.cmd -TargetOpenId`（自提权） |
| **WSL2 Ubuntu** | `ps -p 1 -o comm=` / apt / NodeSource / `npm i -g @larksuite/cli` / `lark-cli config init` & `auth login` / `setup.sh` / `finalize-systemd.sh` / `verify.sh` / `install-dsh-consumer.sh` / `feishu-bridge …` |
| **Mac** | `pytest` / `pack-windows.sh` / `tailscale status` / `ssh` / `check-remote-debug.sh` / `remote_doctor.py` / `bridge-remote.sh` |

## 附 B：故障对照（按踩坑概率）

| 现象 | 根因 | 处理 |
|---|---|---|
| 安装停在退出码 5 | 没做 `lark-cli config init` 或没登录 | P2 补齐后重跑安装 |
| 全绿但 `authenticated=false` | 6 scope 缺 / 审批没过 / 可用范围没含本人 | 回 P0-1.2 补、重新发版审批、增量 `auth login` |
| 收到消息但从不回复 | `sending_enabled=false`（新装默认） | `feishu-bridge resume` |
| 消费者 claim 恒 0 | 消费者没 register / 队列里没有 pending 事件 | `events status` 看 `consumers[]` 与积压；桥不会替消费者回答 |
| `doctor --assert` 报 `consumer_offline` | 有 `message` 积压但没人领取（消费者没起来/没凭证） | 按 §6.5 起消费者；`last_seen_at` 在跳才算好 |
| `MISSING_CREDENTIAL` | 没放 key（或 WSL 侧没登录） | P5-6.4；先凭证再起消费者 |
| `SANDBOX_UNAVAILABLE` | 没有 bwrap 且 Landlock 不可用 | `sudo apt-get install -y bubblewrap` |
| 消费者 `active` 但没心跳 | 凭证/网络/headless 崩溃 | `journalctl -u io.feishu-agent.dsh-consumer -n 50` |
| WSL 断开后服务停 | 只有用户级 unit / holder 没跑 | `verify.sh` 看 system scope；重跑 `install.cmd` |
| 重启后不回消息 | 计划任务没跑 / 未登录 | `schtasks /query /tn FeishuBridgeBoot /v /fo list` |
| 22 连不上 | sshd 没起 / 防火墙 / 不同 tailnet | `Get-Service sshd`、`Get-NetFirewallRule`、`tailscale status` |
| 每 5 分钟闪窗口 | 旧版保活任务重复触发 | 新版安装器已移除；`schtasks /query` 查遗留任务 |
| 登录/每次升级后闪一个 1129×635 黑窗 | 计划任务用 `powershell.exe` 起 launcher，Task Scheduler 为它建控制台窗口 | 重装一次（安装器 2026-11-03 起动作改为 `conhost.exe --headless`）；`Get-ScheduledTask FeishuBridgeBoot` 看 `Execute` 是否为 `conhost.exe` |

## 附 C：与其他文档的关系（避免又出现多套流程）

- 拓扑只有两条（A/B）：[WINDOWS-DECISION.md](../WINDOWS-DECISION.md) 是唯一权威，本文不改它。
- 发行包结构与阶段细节：[dist/wsl2/README.md](../dist/wsl2/README.md)（模式、退出码、Prereq 五步序列的唯一权威）。
- 只装前置环境的通俗说明（WSL2/Node/lark-cli/OpenSSH，不装 bridge）：[docs/windows-prereq-install.md](windows-prereq-install.md)。
- 手工路线、Windows 术语表、退化路径：[INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md)。
- 远程通道与工具：[docs/ops-remote-manage.md](ops-remote-manage.md)。
- 本文只补「依赖清单 + 阶段顺序 + 验收门 + WSL/飞书应用/DSH/Tailscale/OpenSSH 的具体安装配置」。
