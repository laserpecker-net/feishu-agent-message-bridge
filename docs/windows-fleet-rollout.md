# Windows 多机落地手册（同事机批量，形态 A）

> **本文件只回答一件事：多台（>3）时怎么排顺序、怎么记、怎么判断「这台好了没」。**
> 单台怎么做**不在**这里：拓扑与形态 → [WINDOWS-DECISION.md](../WINDOWS-DECISION.md)（唯一权威）；
> 安装、阶段序列与退出码 → [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md) / [dist/wsl2/README.md](../dist/wsl2/README.md)；
> 只装前置环境 → [windows-prereq-install.md](windows-prereq-install.md)；
> 远程通道（Tailscale auth key、节点密钥到期与续期）→ [onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md) §8；
> 日常运维命令 → [ops-remote-manage.md](ops-remote-manage.md)。

## 0. 立场：要统一的是**判据和台账**，不是安装脚本

每台 Windows 的环境都不一样（§1 那张表就是清单：BIOS 虚拟化、家庭版/专业版、已装 Docker、公司代理、
EDR、代码页、中文用户名……）。所以「写一个脚本把 11 台全装完」的结局是确定的：第 3 台加一个 `if`，
第 7 台加一段「先判断再决定走哪条」，最后它长成**发行包里那个安装器的第二套实现**——而
[WINDOWS-DECISION.md](../WINDOWS-DECISION.md) §1 第 1 条禁止的正是这个，[AGENTS.md](../AGENTS.md) §0 也写了
「两套实现并行 = 未完成的重构」。

发行包已经有幂等的 4 个模式（`Install` / `Prereq` / `Check` / `Uninstall`，可在任何状态重跑），
**机器的差异用「先探测 → 选入口 → 记差异」消化，不用新代码消化**。真正必须在 11 台之间保持一致的只有三样：

1. 动手前跑同一组**只读探测**（§1），把这台「特殊在哪」写进台账；
2. 装完跑同一组**验收判据**（§3）——路径可以不同，**结论必须可比**；
3. 同一张**台账**字段（§4），当天填，不靠记忆。

## 1. 动手前：同一组只读探测（管理员 PowerShell，不改任何东西）

```powershell
# 0) 基本信息
Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber
Get-CimInstance Win32_ComputerSystem  | Select-Object Manufacturer,Model,HypervisorPresent

# 1) 虚拟化（BIOS 层）—— 本项目最大的批量卡点
systeminfo | Select-String -Pattern 'Hyper-V','虚拟化','Virtualization'

# 2) WSL 现状（已有发行版？版本是 1 还是 2？）
wsl --status
wsl -l -v

# 3) 已装的虚拟化软件（会抢 Hyper-V、改默认发行版、占资源）
Get-Package | Where-Object Name -match 'Docker|VMware|VirtualBox'

# 4) 磁盘（WSL2 + 依赖 5GB 起，建议 ≥20GB 空闲）
Get-PSDrive C | Select-Object Used,Free

# 5) 权限与代码页（PS 5.1 + CP936 是已知编码坑，见 docs/HISTORY.md §8 踩坑 1/4）
whoami /groups | Select-String 'S-1-5-32-544'     # 在 Administrators 组里？
$PSVersionTable.PSVersion
[Console]::OutputEncoding.CodePage

# 6) 外网（缺一条都正常，但代理会拦 npm/pip/飞书，要记下来）
foreach ($h in 'registry.npmjs.org','pypi.org','www.feishu.cn') {
  $r = Test-NetConnection $h -Port 443 -WarningAction SilentlyContinue
  '{0,-22} {1}' -f $h, $r.TcpTestSucceeded
}

# 7) 杀软 / EDR（非 Microsoft 的要记：可能拦 MSI、改防火墙规则）
Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct |
  Select-Object displayName,productState

# 8) 电源计划（WSL2 随睡眠停 → bridge 的 sender 停；要求「接电源时从不睡眠」）
powercfg /getactivescheme

# 9) 22 端口与已有 sshd（WSL 的 localhost 转发会让端口"看起来在听"，见 HISTORY §8 踩坑 2）
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalAddress,OwningProcess
Get-Service sshd -ErrorAction SilentlyContinue

# 10) 已有的 Tailscale / VPN（登了别人的账号 = 设备归属错，必须先删设备再入网）
Get-Item 'C:\Program Files\Tailscale\tailscale.exe' -ErrorAction SilentlyContinue
Get-VpnConnection -ErrorAction SilentlyContinue | Select-Object Name,ServerAddress,ConnectionStatus
```

探测输出**原样贴进台账的「特殊点」列**。看到下列结果时，按右列处置，不要现场发明方案：

| 探测结果 | 含义 | 处置 |
|---|---|---|
| `HypervisorPresent=False` 且 systeminfo 显示虚拟化已禁用 | BIOS 没开 VT-x/AMD-V | 必须有人坐到机器前开 BIOS；家庭版同样需要。**这一条决定了这台机器能不能远程搞** |
| 装了 Docker Desktop | 自带 WSL2 后端，可能改默认发行版 | 确认 `wsl -l -v` 里 Ubuntu 是 version 2；磁盘按 §4 再留余量；不用卸载 |
| npm/pypi/飞书 有不可达 | 公司代理 / 白名单 | 按 [windows-prereq-install.md](windows-prereq-install.md) §五 处理；**不要把代理写进安装脚本** |
| 代码页 936 + PowerShell 5.1 | 已知编码坑 | 用发行包里带 BOM 的脚本；**别用编辑器另存 `.ps1`**（掉 BOM → `ParserError`，见 HISTORY §8 踩坑 1） |
| 22 端口被非 sshd 进程占用 | 可能是 WSL relay 假阳 | 判据是**读到 `^SSH-` banner**，不是"端口能连上"（HISTORY §8 踩坑 2） |
| 已装 Tailscale 且登了别人的账号 | 设备归属错误 | 管理台先删该设备，再用 §8.0.1 的一次性 key 入网；不要"就地改账号" |
| 电源计划会睡眠/休眠 | 人下班 = bridge 掉线 | 设「接电源时从不睡眠」；这属于环境事实，不是脚本能兜住的 |
| 管理员账户是中文用户名 / 中文路径 | 历史坑区 | 记进台账，装的时候留意路径引号；不额外做转换逻辑 |

## 2. 单台推进的 7 步（每步：谁做 / 通过判据 / 失败出口）

| # | 谁做 | 做什么 | 通过判据 | 失败时 |
|---|---|---|---|---|
| 1 | 你 | 台账先占一行（§4） | 行已存在 | — |
| 2 | 同事/你 | 跑 §1 只读探测 | 「特殊点」列填了东西 | — |
| 3 | 你签 + 同事跑 | 远程通道：[§8.0.1](onboard-windows-dsh-colleague.md) 签 **1 张**一次性 key → 同事跑 [§8.0.2](onboard-windows-dsh-colleague.md) 那两行 | 回执三处（[§8.0.3](onboard-windows-dsh-colleague.md)）：`verdict=OK`、`BackendState=Running` + `100.x`、`listen22=True` | 贴原始报告；`tskey` 只 1 天有效，过期就重签一张（不要复用） |
| 4 | 同事跑 / 你远程跑 | 前置环境：发行包 `prereq.cmd`（= `-Mode Prereq`）；**目标机断网或不想依赖网络**就用离线包 `00→01→02`（[dist/wsl2/README.md](../dist/wsl2/README.md) §3.4） | [windows-prereq-install.md](windows-prereq-install.md) §四 三条命令全绿 | 对照 [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md) §9 已知缺陷；不在列表就停 |
| 5 | 你远程 | 安装：发行包 `install.cmd`（= `-Mode Install`），**显式给 `-Signature`** | `install.py` 退出码 0 + WSL 里 `feishu-bridge status` 的 `workers` 里 `WORKER_NAMES` 各项都在 | 同上；**不要复制别台的 `~/.feishu-personal-agent`** |
| 6 | 同事本人 | 飞书登录（扫码，各人自己的账号） | `status.json` 的 `identity.userName` = 这位同事 | 身份不对 = 登错账号或复制了别台数据，删数据重来 |
| 7 | 你 | 验收（§3）+ 台账收尾 + key expiry 定为 `DISABLED` 或抄下到期日（[§8.0.4](onboard-windows-dsh-colleague.md)） | §3 五条全过 | 不进下一批 |

**失败出口只有一句话**：贴原始输出（含退出码）→ 对照 `INSTALL-WINDOWS.md` §9 已知缺陷 →
不在这张表里就**停下问人**。不要改脚本、不要绕过、不要自写中转（[WINDOWS-DECISION.md](../WINDOWS-DECISION.md) §1 第 1 条）。

## 3. 验收判据（每台同一组，只读）

```bash
# 机名/TP 用户不要手拼：台账是唯一定义处，cards 里就是复制即用的命令
python3 tools/windows_host.py <名字>      # 打印 check / version / logs / ssh 全套（含 magicdns 与 ssh_user）
tools/bridge-remote.sh --host "$H" --user "$U" check      # WSL 里 doctor --assert + WSL verify.sh
tools/bridge-remote.sh --host "$H" --user "$U" version    # installed == running（plist/unit 内容不算数）
```

再加两条人工的：

- 在群里 @ 一次 → 收到 1 条回复，且落款/自称是**这台机器登录的那位同事**（不是别台名字、不是默认人名）；
- Tailscale 节点密钥状态已确认（`DISABLED` 或到期日已进台账，见 §8.0.4）。

**五条全过 = 这台 DONE。任何一条不过，不要开下一台**——11 台的错误会叠加成"到底哪台坏了"。

## 4. 台账：本机 `docs/windows-hosts.csv`（格式在仓库，数据不进 git）

台账的字段、登记流程、live/登记值的优先级，全部在 [windows-hosts.md](windows-hosts.md)。
**机名 / MagicDNS / Tailscale IP / SSH 用户 / 飞书 openId 只在那张表里定义**，本文件和其他文档一律链接过去，
不复制——否则以后"某位同事是哪台机器"会有两个答案。

数据文件（`docs/windows-hosts.csv`）**是本机文件，已在 `.gitignore`**：里面有同事姓名与飞书 `open_id`，
进 git 就等于发到远端、以后删不干净。仓库里只保留模板 [windows-hosts.example.csv](windows-hosts.example.csv)。

对流程只提两条硬要求：

1. **开工前先占一行**（还没装就先登记 `status=待部署`），否则装完就忘、以后对不上人和机器；
2. **§3 五条全过后**才把 `status` 改成 `在线`/`OK`，没验收的不许写成已完成。

查一台机器：`python3 tools/windows_host.py <名字>`（台账 + 本机 `tailscale status` live 覆盖，
顺带打印 `bridge-remote.sh` / `remote_doctor.py` / `ssh` 的复制即用命令）。

## 5. 批次节奏

| 批 | 内容 | 目的 |
|---|---|---|
| 批 0 | 已跑通的参照机（第一台试点机） | 只作对照，不进队列 |
| 批 1 | **2 台，故意选不同品牌 / 不同 Windows 版本** | 验证发行包在异质环境仍能过 §3。发现差异 → 写回本文件的 §1 表，**不改脚本** |
| 批 2-4 | 各 3 台（同品牌可并批） | 流水线化 |
| 收尾 | 剩余 | — |

- 时间：同事前置 10-15 min/台（[§8.0.2](onboard-windows-dsh-colleague.md) 那段话），你远程 20-30 min/台；
  串行约 1-1.5 人天，2-3 台并行可压到半天。
- **auth key 按批签**（1 天过期）：一次签 11 张 = 第二天 10 张作废。每批 2-3 张，当天发当天用。

## 6. 批量动作：只做只读复核

台账在 `docs/windows-hosts.csv`，所以复核直接以它为输入（不要另抄一份主机清单）：

```bash
# 1) 一览：谁在线、谁几号到期（live 来自本机 tailscale）
python3 tools/windows_host.py --all

# 2) 逐台只读体检（version = installed/running 是否一致；check = WSL 里 doctor --assert + verify.sh）
python3 tools/windows_host.py --all --json | python3 -c "
import json, subprocess, sys
for h in json.load(sys.stdin):
    host, user = h['magicdns'], h['ssh_user']
    ver = subprocess.run(['tools/bridge-remote.sh','--host',host,'--user',user,'version'],
                         capture_output=True, text=True)
    chk = subprocess.run(['tools/bridge-remote.sh','--host',host,'--user',user,'check'],
                         capture_output=True)
    print(f\"{h['name']:<8} {host:<40} version={ver.returncode} check={chk.returncode}\")
"
```

**写入类动作（`upgrade` / `hold` / `uninstall`）逐台做，不并批**：11 台同时升级出问题时，
你分不清是发行包的问题还是某一台机器的问题。

## 7. 明确不做（每条都有代价）

| 不做 | 为什么 |
|---|---|
| 为多台写「统一安装脚本」 | 会变成发行包安装器的第二套实现（[AGENTS.md](../AGENTS.md) §0）；4 个模式已幂等可重跑 |
| 跳过 §1 探测直接跑 prereq | 11 台会得到 11 种无上下文报错，分不清「机器特殊」还是「已知缺陷」 |
| 复制别台的 `~/.feishu-personal-agent` / `config.json` | 身份是账号事实，复制 = 10 台对外自称错人（HISTORY §4 的真实事故） |
| 首批就顺手做 tag / ACL 改造 | 与安装混在一起，出问题分不清是网络策略还是安装；先关 key expiry，隔离留到第二批之后 |
| 拖到到期最后一天才续 Tailscale key | 节点密钥过期后远程手段全失效，只剩坐到机器前（[§8.0.4](onboard-windows-dsh-colleague.md)） |
| 同一台机器跑两个 bridge / 两个消费者 | 双写 SQLite、双领事件（[WINDOWS-DECISION.md](../WINDOWS-DECISION.md) §1、[CONTRACTS.md](../CONTRACTS.md)） |

## 8. 预期管理：11 台 = 11 份独立系统

- 独立 SQLite、独立飞书身份、独立 release 副本。**任何"改一处生效 11 处"的期待都不成立**。
- 升级是 11 次（逐台 `bridge-remote.sh upgrade`），体检是 11 次（§6 循环）。所以 §6 的循环要在
  批 1 就跑顺，否则以后每次发版都是半天手工。
- Tailscale 额度不是瓶颈（设备走 auth key，不占用户席位；免费额度 100 台设备 / 3 用户），
  **成本在人的时间，不在额度**。
