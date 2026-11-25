# Windows 前置环境安装说明（一条命令，只装资源、不装服务）

> 给拿到一台新 Windows、要把它变成「能跑飞书桥接 + AI 助手」的同事看的。
> 这份说明只讲**装环境**这一件事；装 bridge 服务、配飞书应用是下一步，文末有指路。
> 命令与退出码的权威定义在 [dist/wsl2/README.md](../dist/wsl2/README.md)，本文不重复表格。

---

## 一、先说清楚：这个脚本到底干什么

一句话：**把「这台 Windows 里有一个能跑 Linux 工具链的 WSL2 环境」这件事，按依赖顺序一次装好。**

它不装飞书桥接服务（bridge），不登录飞书，不碰你已有的数据。装完以后机器是「准备好的空机器」，
后面装 bridge 只是往里放一个服务，不会再折腾环境。

按依赖顺序，它会做这几件事（前一件不做完，后一件做不了）：

```text
1. 检查 Windows 版本、是否开了虚拟化、有没有管理员权限
2. 装 WSL2（Windows 里的 Linux 子系统）
3. 装 Ubuntu 发行版，并确认有一个普通用户（不是 root）
4. 在 Ubuntu 里打开 systemd（让 Linux 能托管后台服务）
5. 装 apt 基础依赖：python3 / sqlite3 / curl / ca-certificates / git / npm
6. 装 Node ≥ 18（lark-cli 要求；缺就装 Node 22）
7. 装 lark-cli（飞书命令行，npm 全局包 @larksuite/cli）
8. 装 Windows OpenSSH Server（以后同事能远程帮你排查）
9. 逐项体检：上面每一件都真的能用，才算通过
```

顺序里 5→6→7 由仓库里的 `dist/wsl2/setup.sh --deps-only` 完成，脚本不另写一套 apt/npm 逻辑。
权威的阶段划分与退出码见 [dist/wsl2/README.md](../dist/wsl2/README.md)（§3.2 / §3.3.1）。

它**不会**做：装 bridge、`lark-cli` 登录、飞书应用配置、回复署名、开机保活、DSH 消费者、Tailscale。
这些是下一步，见本文末尾。

---

## 一之二、目标机没有外网怎么办（离线安装包）

上面这条命令默认会联网（apt 源 / NodeSource / npm registry）。**目标机断网、或公司白名单不可靠时**，
用离线安装包：所有依赖（Ubuntu 镜像、WSL MSI、Node、lark-cli、sqlite3 的 apt 包、OpenSSH MSI）都在包里，
装的时候不问网络。三个文件按顺序双击，每个都自带管理员提权：

| 顺序 | 文件 | 通过标志 | 说明 |
|---|---|---|---|
| 1 | `00-自检（先跑这个）.cmd` | `PREFLIGHT … bad=0` | 只读：机器现状 + 载荷完整性（逐文件 sha256） |
| 2 | `01-装前置环境（右键以管理员运行）.cmd` | `PREREQ_DONE … exit=0` | 装 WSL2/Ubuntu/systemd/Node/lark-cli/OpenSSH |
| 3 | `02-验收（不需要管理员运行）.cmd` | `VERIFY … exit=0` | 只读断言（= `-Mode PrereqCheck`） |

两条硬规则：

1. **先把这个文件夹整个复制到本机磁盘**（例如 `D:\feishu-kit`）再双击——从 NAS 共享路径提权会失败；
2. 任何一步不是 `bad=0` / `exit=0`：把窗口最后 30 行原样发回对接人，别自己绕过（同 §一 的立场）。

离线包怎么生成、包内结构、载荷版本表、会动到什么/怎么撤销：见 [dist/wsl2/README.md](../dist/wsl2/README.md) §3.4。
---

## 二、动手前的 4 项检查（2 分钟）

| 检查项 | 怎么确认 | 不满足会怎样 |
|---|---|---|
| Windows 版本 | `winver`：Win10 2004+（build ≥ 19041）或 Win11 | 装不了 WSL2，脚本会直接报错停下 |
| 虚拟化已开 | 任务管理器 → 性能 → CPU → 看「虚拟化：已启用」 | WSL2 起不来；要进 BIOS 打开 VT-x/AMD-V |
| 管理员权限 | 你能右键「以管理员身份运行」 | 脚本会弹 UAC；公司策略禁止就装不了 |
| 磁盘空间 | 至少 5 GB 空闲 | WSL2 + 依赖装到一半失败 |

网络要能访问：apt 源、`registry.npmjs.org`、飞书开放平台；装 OpenSSH 的兜底路径可能访问 GitHub。
公司有代理/白名单的话，先确认这几处能通，否则会卡在第 5 步。

仓库**不能**放在 `C:` 盘的 Windows 目录里跑服务——WSL2 里会自己复制一份到家目录，这一步脚本代做，
你不用管；但如果你手动拷代码，记得只放在 WSL 的家目录（`/mnt/c/...` 会被拒绝）。

---

## 三、怎么跑（一条命令）

### 3.1 用发行包（推荐，同事就是双击）

1. 解压 `feishu-bridge-windows-<日期>.zip` 到任意目录。**路径别带中文和空格**更省事（例如 `D:\feishu-kit`）。
2. 右键 **`prereq.cmd`** → 「以管理员身份运行」。
3. 中间最多停两次等人，照着窗口里的话做：
   - **Ubuntu 第一次启动**：会弹出一个 Ubuntu 窗口，按提示输入 UNIX 用户名和密码（**不要用 root**，
     也不要和 Windows 用户名混为一谈），看到 shell 提示符后关掉，回到安装窗口按 Enter。
   - **sudo 要密码时**：会弹一个 WSL 窗口让你输密码，装完回到安装窗口按 Enter。
   - 其余时间不用动。首次大约 10–20 分钟（看网络），重跑只补缺项，通常 1–2 分钟。
4. 看到「**前置资源就绪**」就是装完了。桌面会留一份 `feishu-bridge-install-<时间>.md` 报告。

### 3.2 用仓库（自己人或调试）

在**管理员** PowerShell 里：

```powershell
# 发行包解压目录：脚本在 repo\dist\wsl2\ 下
powershell -NoProfile -ExecutionPolicy Bypass -File .\repo\dist\wsl2\install-windows.ps1 -Mode Prereq

# 仓库工作树：脚本在 dist\wsl2\ 下
powershell -NoProfile -ExecutionPolicy Bypass -File .\dist\wsl2\install-windows.ps1 -Mode Prereq
```

可选开关：

| 开关 | 作用 | 什么时候用 |
|---|---|---|
| `-SkipOpenSsh` | 不装 Windows OpenSSH Server | 公司策略不许开 sshd，或你另有远程通道 |
| `-AllowLanSsh` | 防火墙额外放行本机所在局域网 | 没有 Tailscale、又想让同网段同事连进来调试 |
| `-Distro <名字>` | 指定 WSL 发行版名（默认 `Ubuntu`） | 机器上已有别的发行版，`wsl -l -q` 看真名 |

> 权限：Prereq 要装 WSL 和 sshd，**必须管理员**。只有 `-Mode Check` 是只读、不需要管理员。
> 重跑是安全的：每一步先探测，已完成的不重复施工；不会覆盖你已有的 WSL 发行版和用户。

---

## 四、装完怎么确认（30 秒）

打开 PowerShell（这次不需要管理员），逐条跑：

```powershell
# 1) WSL 里：systemd / 普通用户 / Node / lark-cli
wsl -d Ubuntu -- bash -lc 'ps -p 1 -o comm=; id -u; node -v; lark-cli --version'

# 2) Windows 侧：sshd 服务
Get-Service sshd | Select-Object Name, Status, StartType
```

期望看到：

```text
systemd          ← 第 1 行
1000（或别的 ≥1000 的数字，不是 0）
v22.x.x          ← ≥18 即可
lark-cli version x.y.z
...
Running  Automatic   ← sshd
```

安装器自己也会跑同样的断言（Prereq 的最后一步），报告里每一行 `[ok]` 就是一条：
`pid1=systemd`、`uid=1000`、`python3 sqlite3 curl git npm`、`node=v22.x`、`lark-cli=…`、`sshd Running`、`22 端口在 listen`。

> 注意：`-Mode Check` 检查的是**整套安装**（bridge 服务也在内）。只装前置环境时它会在 bridge 相关项上报缺失，
> 那是正常的——前置资源自己的通过标准以 Prereq 最后一步的输出为准。

---

## 五、没通过怎么办

**第一原则：把窗口最后 30 行原样贴给对接人，不要自己改脚本、不要自己造替代方案。**
安装器失败时最后一行一定是 `[fail] ...`，报告文件里有全部阶段结果。

常见的几种，按现象对：

> 下表**只列现象与处置，不是退出码表**：退出码的含义全仓只有一份，见
> [dist/wsl2/README.md](../dist/wsl2/README.md) §3.2。这里出现数字只是帮你对上脚本打印的那一行。

| 现象 | 意思 | 你该做什么 |
|---|---|---|
| 窗口最后一行是 `3010` | 刚装完 WSL，Windows 需要重启 | 重启后**原样重跑**，已完成的会跳过 |
| `Windows build ... 过低` | Windows 版本不够，或虚拟化没开 | 升级 Windows 或进 BIOS 开虚拟化 |
| `Ubuntu 发行版还没就绪` / 没有普通用户 | Ubuntu 首次初始化没做完 | 开始菜单打开 Ubuntu，建完用户再重跑 |
| systemd 没起来 | `/etc/wsl.conf` 没生效 | 按脚本打印的手工命令检查 `/etc/wsl.conf`；**不要**改 `%USERPROFILE%\.wslconfig`，那里没有 `[boot]` |
| 代码树不完整 | 发行包缺文件 | 用完整的发行包，别只拷单个脚本 |
| 依赖装失败 | apt 网络/镜像问题，或 lark-cli 没装上 | 看 setup.sh 最后几行：404/超时=网络或镜像；`lark-cli: command not found`=npm 全局 bin 不在 PATH |
| sshd 装不上 | 可选功能被策略禁用，MSI 兜底也失败 | 看桌面 `openssh-install-log.txt`；公司策略禁止就加 `-SkipOpenSsh` 跳过 |
| 体检有一项没过 | 看脚本打出的 `[fail]` 那条 | 修完重跑；它明确失败而不是"差不多能用" |

---

## 六、安全边界（别踩）

- **22 端口默认只对 Tailscale 网段放行**。装 sshd 时会停用 Windows 自带那条「允许所有来源」的规则，
  所以「装了 sshd」≠「现在就能被别人 ssh 进来」：还需要授权公钥（见
  [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md) §13.2）。
- 没有 Tailscale、又确实需要同网段调试，才加 `-AllowLanSsh`；**永远不要**把 22 直接暴露到公网。
- 前置阶段不写你的飞书凭证、不登录、不改 `~/.feishu-personal-agent` 里任何数据。
- 脚本可以反复跑；不会重装已有的 WSL 发行版，也不会删你 Ubuntu 里的文件。

---

## 七、下一步：装 bridge（这才是能让飞书 @ 有回复的部分）

环境就绪后，还差三件事（都需要人做，脚本代替不了）：

1. **飞书开放平台建企业自建应用**，开 5 个 scope 并通过发布审批
   （清单与截图步骤见 [docs/onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md) §1.2）。
2. **在 WSL2 里绑定应用并登录**：

   ```bash
   printf '%s' '<App Secret>' | lark-cli config init --app-id cli_xxx --brand feishu --app-secret-stdin
   lark-cli auth login --scope "im:chat:read im:message im:message.group_msg:get_as_user im:message.p2p_msg:get_as_user im:message.send_as_user search:message"
   lark-cli auth status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["identities"]["user"]["openId"])'
   ```

   最后一行输出的 `ou_...` 就是你的 open_id，装 bridge 时要它。
3. **装 bridge**：右键 `install.cmd`（管理员）→ 在打开的 WSL 窗口里完成登录 → 看到「安装完成」。
   验收：`check.cmd`，或 WSL2 里 `feishu-bridge status`。

> 想接 AI 回复引擎（DSH）的话：Prereq 装的 Node 是 ≥18（lark-cli 的要求），DSH 要 **Node 22**，
> 升级步骤在 [onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md) §6.1（一句 `nvm`/官方脚本，不影响已装的东西）。

想看「从裸机到群里 @ 能回复」的完整分阶段手册：见
[docs/onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md)；
bridge 安装的细节与所有退出码：见 [dist/wsl2/README.md](../dist/wsl2/README.md)。

---

## 八、相关文档（避免多套说法）

| 想了解 | 看哪份 |
|---|---|
| Windows 走哪条拓扑（A/B） | [WINDOWS-DECISION.md](../WINDOWS-DECISION.md)（唯一权威） |
| 发行包用法、模式、退出码、阶段细节 | [dist/wsl2/README.md](../dist/wsl2/README.md)（唯一权威） |
| 手工路线、术语表、各平台差异 | [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md) |
| 从裸机到能回复的分阶段交接 | [docs/onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md) |
| 以前踩过的坑、为什么这么定 | [docs/HISTORY.md](HISTORY.md) |
