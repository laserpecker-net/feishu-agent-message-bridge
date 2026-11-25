# 同事机台账（「人 → 机器 → 通道」的唯一来源）

> **台账数据是本机文件 `docs/windows-hosts.csv`，绝不进 git**（含同事姓名 / 飞书 `open_id`，已在 `.gitignore`）。
> 仓库里只保留格式模板 [windows-hosts.example.csv](windows-hosts.example.csv)（示例行不是真数据）。
> 定位一台机器只要一条命令：`python3 tools/windows_host.py <名字>`。
> 部署顺序与探测清单不在本文件 → [windows-fleet-rollout.md](windows-fleet-rollout.md)；
> 安装与退出码 → [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md) / [dist/wsl2/README.md](../dist/wsl2/README.md)；
> 远程通道怎么建立 → [onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md) §8。

## 0. 为什么必须有这张表

「某位同事的电脑」不是一个任何系统能解析的标识符：机器名是 `example-win` 这种随机串，Tailscale 里认的是
MagicDNS/IP，飞书侧认的是 `ou_...`，SSH 认的是 Windows 账户名。**只有这张表能把「人」和这四个标识符钉在一起。**

**为什么是本机文件而不是仓库文件**：这张表天生是个人信息（姓名 + 飞书 `open_id` + 内网地址），
进 git 就等于发到远端、留在历史里、以后删不干净。所以仓库只版本化**格式**（模板 + 本文件 + 查询工具），
数据留在本机。代价是换电脑 / 新克隆时表是空的——照 [§3 第 0 步](#3-登记一台新机器装完当场做别攒着) 用模板重建即可，
十几台也就是几分钟的事。
所以这里是机名 / MagicDNS / Tailscale IP / SSH 用户 / 飞书身份 / release 的**唯一定义处**，其他文档只链接、不复制。

典型用法：同事报「某某的电脑不回复了」→ 在仓库根目录跑一条查询 → 拿到机器、通道命令、到期日 → 直接开始排查。

## 1. 一眼定位

```bash
python3 tools/windows_host.py <名字>          # 人/飞书名/机名/MagicDNS/IP/SSH 用户都能当 query
python3 tools/windows_host.py --all           # 全体一行一台：称呼 / 机名 / 在线 / key expiry
python3 tools/windows_host.py example-win --json
```

输出卡片里：

- `key expiry` 后面标 **`[live]`** = 来自本机 `tailscale status --json`（权威），标 **`[登记值]`** = 读不到 live 时的降级值；
- 「通道」块是复制即用的命令（`bridge-remote.sh check/version/logs`、`remote_doctor.py`、`ssh`、进 WSL）。

退出码：`0` 命中；`1` 台账里没有这个人/机器；`2` 台账缺失或缺列（参数错误）。

## 2. 字段（CSV 列，顺序可变、列不能少）

| 列 | 含义 | 从哪来 |
|---|---|---|
| `name` | 你怎么称呼这位同事（口头就用它） | 你 |
| `feishu_user` | bridge `status.json` 的 `identity.userName` | `feishu-bridge status` |
| `feishu_open_id` | 同上的 `identity.openId`（`ou_...`） | `feishu-bridge status` |
| `machine` | Windows 计算机名 = Tailscale HostName | `hostname` / `tailscale status` |
| `magicdns` | `<machine>.tailnet.ts.net`；**ssh 优先用它，别用 IP** | `tailscale status --json` 的 `DNSName` |
| `tailscale_ip` | `100.x` | `tailscale ip -4` |
| `ssh_user` | Windows OpenSSH 登录用户（必须是 Administrators 组成员） | 装机时定的 |
| `wsl_distro` / `wsl_user` | WSL 发行版 / 里面的普通用户 | `wsl -l -v`、`wsl -d <发行版> -- whoami` |
| `os` | Windows 版本 + 机型（一句话，出问题时用来判断差异） | `Get-CimInstance Win32_OperatingSystem` |
| `deployed_at` | 通道/首装日期 | 装机当天 |
| `release` | bridge 当前 release_id | `~/.feishu-personal-agent/installation.json` 的 `release_id` |
| `key_expiry` | Tailscale 节点密钥到期日；`DISABLED` = 管理台已关 | [onboard 文档 §8.0.4](onboard-windows-dsh-colleague.md) |
| `status` | 登记快照：在线 / 离线 / 已退网 / 已转交 | 验收时 |
| `notes` | 这台的**特殊点**（探测结论，如「BIOS 虚拟化已开；无 Docker；会睡」） | [fleet §1](windows-fleet-rollout.md) 探测 |

## 3. 登记一台新机器（装完当场做，别攒着）

0. **本机还没有台账文件**（新克隆 / 换电脑）：
   `cp docs/windows-hosts.example.csv docs/windows-hosts.csv`，然后删掉里面那行示例数据；
   文件已列入 `.gitignore`，`git status` 里应该始终看不到它——如果在 `git status` 里看到，说明 `.gitignore` 被改坏了。
1. 先按 [windows-fleet-rollout.md](windows-fleet-rollout.md) §1 跑只读探测，把差异写进 `notes`/`os`；
2. 按同文件 §3 验收，五条全过后再登记（没验收的不许进表，否则表就变成"愿望清单"）；
3. 三个事实**抄机器输出，不要手打**：
   - 机名/MagicDNS/IP：`tailscale status`（或 `tailscale status --json`）；
   - 飞书身份：`ssh <ssh_user>@<magicdns> 'wsl.exe -d Ubuntu -- bash -lc "feishu-bridge status"'` 里的 `identity`；
   - release：WSL 里 `cat ~/.feishu-personal-agent/installation.json` 的 `release_id`（或 `readlink ~/.feishu-personal-agent/current`）；
4. 追加一行 CSV，再跑 `python3 tools/windows_host.py <name>` 确认能命中、并且 `[live]` 那一行不是「未知」。

## 4. 会漂移的列：`key_expiry` / `release` / `status`

这三列是**登记快照**，迟早就和现场不一致。判据：**以 `tools/windows_host.py` 打印的 `[live]` 为准**；
不一致时改的是 CSV（数据），不是代码。`release` 没有 live 通道，逐台 `bridge-remote.sh version` 核对。

## 5. 边界

- **数据不出版本库**：`docs/windows-hosts.csv` 在 `.gitignore` 里；仓库里能提交的只有模板、本文件与查询工具。
  别为了"大家都能查到"把它 `git add -f` 进去——那是把姓名和 `open_id` 写进远端历史，撤不回来。
- 含同事姓名与飞书 `openId`：也不要贴进群、不要进对外文档。
- 机器退网 / 换人：把 `status` 改成 `已退网` / `已转交`，**不要直接删行**——以后"那台老机器当年是什么配置"还要靠它。
- 一人多台：同一 `name`、不同 `machine`，各占一行。
- 身份事故的背景（为什么不能用别台的名字/配置）见 [docs/HISTORY.md](HISTORY.md) §4。
