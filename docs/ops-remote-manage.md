# 远程运维手册：Tailscale + OpenSSH（所有宿主）

目标：**不用坐到那台机器前面**，就能看状态、体检、挂起/恢复、升级、回滚、拆装。
本文只讲通道与命令，不重复各平台的安装细节（macOS → [INSTALL-MACOS.md](../INSTALL-MACOS.md)，Windows → [INSTALL-WINDOWS.md](../INSTALL-WINDOWS.md)）。

## 1. 通道拓扑（实测，2026-11-02）

| 宿主 | Tailscale 节点 | 进入 bridge 所在 shell 的方式 |
|---|---|---|
| macOS | `<mac-tailscale-ip>`（MagicDNS `*.<tailnet>.ts.net`） | `ssh <user>@<magicdns>` → 直接是 POSIX shell |
| Windows + WSL2 | `<windows-tailscale-ip>` | `ssh <user>@<magicdns>` → Windows OpenSSH（cmd）→ `wsl.exe -d <发行版> -- bash -s` |

要点：

- **Tailscale 只负责网络层**（MagicDNS 名字、设备认证、NAT 穿透）。SSH 认证仍是 OpenSSH，本工具链不引入额外依赖。
- **同事机怎么入网**：你签一张一次性 auth key，同事跑一条命令即可（不需要他的 Tailscale 账号，也不要让他登你的账号）——
  流程只在 [onboard-windows-dsh-colleague.md](onboard-windows-dsh-colleague.md) §8.0 定义，本文不重复。
- Windows 原生不提供 Tailscale SSH server，所以必须装 **Windows OpenSSH Server**；本机已实测 `sshd` 服务 `Running/Automatic`、只允许密钥登录（`PasswordAuthentication no`）。
- **Windows 侧 22 端口只对 Tailscale 网段开放**：认**规则名** `OpenSSH-Server-Tailscale-In-TCP`（DisplayName 会随
  `-AllowLan` 变成 `OpenSSH Server (Tailscale only)` / `(Tailscale + LAN)`，别拿 DisplayName 当标识），
  RemoteAddress 是 `100.64.0.0/10` + `fd7a:115c:a1e0::/48`；系统自带的「所有来源」22 端口规则（Name 可能是
  `OpenSSH-Server-In-TCP`，也可能是 GUID）由 `tools/windows/install-openssh.ps1` 按**端口 22 + 来源 Any** 找出来停用，
  停不掉就让安装失败 —— 别为了图方便把它改成 Any。
  用离线包 `win-ssh-bootstrap` 起步的机器，包自带规则是 `Profile Any` 全开 + 密码登录保留，装完必须跑 `tools/windows/ssh-bootstrap/harden-tailscale.ps1 -DisablePassword` 收敛到这一条（见 NOTES.md §5）。
- macOS 侧**默认没有**远程登录：`launchctl print system/com.openssh.sshd` → `state = not running`。要远程管理 Mac，先在 系统设置 → 通用 → 共享 → 远程登录 打开（需要管理员），并确认防火墙放行 Tailscale 接口。

一条连通性自检（在管理机上跑）：

```bash
ssh -o BatchMode=yes <user>@<win-magicdns> 'echo %USERNAME%'        # Windows 侧
ssh -o BatchMode=yes <user>@<win-magicdns> 'wsl.exe -d Ubuntu -- uname -a'   # 进 WSL
ssh -o BatchMode=yes <user>@<mac-magicdns> 'uname -s'               # macOS 侧（需先开远程登录）
```

## 2. `tools/bridge-remote.sh`：一条命令搞定日常

脚本只用本机 `ssh`/`scp`，不动对端任何东西（除 `upgrade`/`uninstall`）。命令正文走 stdin（`bash -s`），不拼引号；PowerShell 动作则上传 `.ps1` 用 `-File` 跑，绕开 cmd.exe 的引号地狱。

```bash
export BRIDGE_WIN=<win-magicdns>   # 示例：win-box.tailnet.ts.net
export BRIDGE_MAC=<mac-magicdns>
export BRIDGE_USER=<你的 ssh 用户>

# 只读
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" status
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" doctor --assert
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" check      # doctor --assert + WSL 侧 verify.sh
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" version    # installed vs running release
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" logs 120
tools/bridge-remote.sh --host "$BRIDGE_MAC" --user "$BRIDGE_USER" status     # macOS 宿主（--target mac 用于 check 以外动作）
```

动作与语义：

| 动作 | 做什么 | 会改状态吗 |
|---|---|---|
| `status` / `version` / `logs [n]` | `feishu-bridge status` / release 对照 / 最近日志 | 否 |
| `doctor [--assert]` | 跨平台体检（`--assert` 有 fail 则退出码 1） | 否 |
| `check` | WSL 里跑 `doctor --assert`（失败 5s 后重试一次）**再**跑 WSL 的 `verify.sh`，退出码取两者最大（doctor 非 0 优先）。Windows 宿主侧没有 `doctor`，这边的对等物是 `install-windows.ps1 -Mode Check`（计划任务/holder/托管） | 否 |
| `ps-tests` | Windows：跑发行包自带两套 PowerShell 测试（需先 `upgrade` 过） | 否 |
| `hold [原因]` / `release` | 挂起（零采集/零入队/零回复）/ 恢复（窗口内消息不补答，回执报丢弃条数） | **是** |
| `upgrade <zip>` | 上传发行包 → 解压到 `C:\ProgramData\feishu-bridge\pkg` → `install-windows.ps1 -Mode Install` | **是** |
| `uninstall` | `install-windows.ps1 -Mode Uninstall`（拆托管与保活，不删数据） | **是** |

`--dry-run` 只打印将执行的远端命令；Windows 目标自动走 `wsl.exe`，`--distro` 可指定发行版。

## 3. 升级（发新版本时）

**Windows 宿主（推荐路径，已实测）**：

```bash
bash dist/wsl2/pack-windows.sh                     # 产出 dist/windows/feishu-bridge-windows-<ver>.zip
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" \
  upgrade dist/windows/feishu-bridge-windows-<ver>.zip
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" check   # 退出码 0 才算成功
```

**升级前先给状态库拍快照**（schema 迁移只能前进、没有 down 脚本；升级之后再拍就是新版本的，回退用不上）。
在目标宿主的 WSL 里跑，`VACUUM INTO` 出来的是一致副本，不用停服务：

```bash
python3 - <<'PY'
import os, sqlite3, pathlib, time
# 两端只差 home 路径：WSL2/Linux 是 ~/.feishu-personal-agent；macOS 先
#   export BRIDGE_HOME="$HOME/Library/Application Support/FeishuPersonalAgent"
home = pathlib.Path(os.environ.get('BRIDGE_HOME') or (pathlib.Path.home() / '.feishu-personal-agent'))
source = sqlite3.connect('file:%s?mode=ro' % (home / 'state.sqlite3'), uri=True)
version = source.execute('PRAGMA user_version').fetchone()[0]
target = home / 'backups' / ('state-v%d-pre-upgrade-%s.sqlite3' % (version, time.strftime('%Y%m%d-%H%M')))
target.parent.mkdir(exist_ok=True)
source.execute('VACUUM INTO ?', (str(target),))
print(target, 'integrity', sqlite3.connect(str(target)).execute('PRAGMA integrity_check').fetchone()[0])
PY
```

**只用 `VACUUM INTO`，不要 `sqlite3 .backup` 也不要 `cp`**：`VACUUM INTO` 出来的副本是自包含的
`journal_mode=delete`（放多久都不会长出旁文件）；`.backup` 会**继承源库的 WAL 模式**——之后任何一次
`sqlite3 <副本>` 查询都会就地生成 `<副本>-wal` / `<副本>-shm`，归档目录里就多出看起来像数据、
其实只是缓存的碎片（2026-11-06 在同事 Mac 上踩到，见 HISTORY §27）。快照统一放 `<home>/backups/`，
不要放 home 根目录——根目录只放服务的活文件。

安装器是幂等的：`Install` 模式只补做缺的阶段；配置（`config.json`）、身份、`hold.json` 的文件内容不会被覆盖，署名不传就沿用既有 config。
**`state.sqlite3` 是例外**：文件不会被替换，但它会**就地迁移到当前 schema**（本次 2.0 是 v3→v6：加 `tasks`/`task_messages`/`events.used_ids`），
迁移只有单向、没有 down 脚本，升级后旧代码打不开这个库。所以"升级不动库"别当成回滚依据——回滚必须连库一起退（§4）。

**macOS 宿主**：

```bash
git -C <repo> pull            # 或直接把新代码放到仓库目录
python3 install.py --verify   # install → restart → 等心跳 → doctor --assert
python3 agentkit-consumerctl.py status   # 若用 agentkit 入口
```

macOS 上没有「发行包」这一层：`install.py` 从当前工作树构建 release，切换 `current` 后再 restart。旧 release 保留最近 10 个（`FEISHU_RELEASE_KEEP`），可回滚。

## 4. 回滚

```bash
# macOS
feishu-bridge rollback --list
feishu-bridge rollback --to <release_id>          # 校验 digest，损坏拒绝
feishu-bridge restart
python3 agentkit-consumerctl.py restart           # 若用 agentkit

# Windows（发行包安装的机器：家目录没有 .git，只能整包换）
# 顺序不能反：2.0 起 store 有硬门禁（旧代码 `user_version > 旧 SCHEMA_VERSION` 直接 RuntimeError，
# systemd `Restart=always` 会变成崩溃循环）。所以"回滚"= 先退库，再退代码。
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" hold --by ops   # 先挂起，别再收消息
# 停服务（WSL 里）：sudo systemctl stop io.feishu-agent.bridge.service io.feishu-agent.dsh-consumer.service
# 再恢复升级前的库（升级时那次 sqlite backup，别用 cp 拷 WAL 态文件）：
#   cp <state-v?-升级前>.sqlite3 ~/.feishu-personal-agent/state.sqlite3
#   rm -f ~/.feishu-personal-agent/state.sqlite3-wal ~/.feishu-personal-agent/state.sqlite3-shm
tools/bridge-remote.sh --host "$BRIDGE_WIN" --user "$BRIDGE_USER" upgrade <旧版 zip>
# 或在 WSL 里：feishu-bridge rollback --list / --to <id>，再 systemctl restart
```

要点：**切完必须 restart** 才生效。旧 release 只有在与当前 SQLite schema 兼容时才能只切代码；
库版本已经前进（例如 v3→v6）时，**必须同时恢复升级前的库**，否则就是崩溃循环——"不要删库"不够，
"别拿旧代码去开新库"才是那条边界。

没按 §3 拍快照、又必须退库时，只剩一条**纯加表型迁移**的捷径（2026-11-06 在 v6→v7 上实测）：

```bash
# 先备份当前库，再摘掉新版本加的表，把版本号退回旧代码认识的数字
sqlite3 "$H/state.sqlite3" "VACUUM INTO '$H/backups/state-before-revert.sqlite3'"
sqlite3 "$H/state.sqlite3" "DROP TABLE IF EXISTS reminders; DROP INDEX IF EXISTS reminders_due; PRAGMA user_version=6;"
# 判据：用 SCHEMA_VERSION=6 的旧代码打开这份库，行数与状态分布不变、PRAGMA integrity_check = ok
```

v3→v6 那种迁移没有这条捷径：它不只是加表加列，还**改写了状态名**
（`analysis_completed` → `reply_queued`，v3→v4 的 `UPDATE`）——只退 `user_version` 的话，
旧代码会读到自己不认识的 `reply_queued` 行。这类升级必须靠升级前的快照。

## 5. 出问题时按这个顺序看

0. `feishu-bridge consumer status`：`healthy=false` 时先看 `hold.active`。挂起期间消费者按语义
   不领事件、也不再刷新 `last_seen_at`，此时 `idle_expected=true` 属预期；没有挂起才是消费者真的没在领。
1. `doctor --assert` → 看哪一项是 fail（服务/心跳/认证/release/worker/agent 可执行性）。
2. `logs --all --lines 200` → 业务日志没线索时看后端日志（导入错误、被 OOM/KILL 都在 launchd/systemd 的 stderr）。
3. `version` → `update_pending=true` 说明「装了新版但进程还是旧版」，`restart` 就解决；不 restart，`hold` 会拒绝执行。
4. Windows 专属：`check` 会跑 WSL 里的 `verify.sh`（systemd 作用域、holder 任务、心跳、`mode`、`dropped`），有问题按它打印的 hint 走。
5. 连不上：先 `tailscale status`（两边是否 Online、是否 direct），再 `ssh -v` 看是网络还是认证；Windows 侧确认 `sshd` 是 Running 且防火墙规则仍是 Tailscale-only。

## 6. 安全边界（别做的事）

- 不要把 8766 / 8768 暴露到 LAN/公网；它们只应绑 `127.0.0.1`（远程消费走 SSH 隧道或 Tailscale 内网 + 端口转发）。
- 不要把 Windows 的 SSH 防火墙规则放宽成 `Any`；需要临时互通就用 Tailscale，不要开公网 22。
- `gateway-token`、`config.json`、`state.sqlite3` 都是敏感文件（token/消息内容），远程拷贝走 `scp`，别贴到聊天里。
- 远程执行 `install-windows.ps1 -Mode Uninstall` 会拆掉 systemd 托管与 holder 任务，但**不删数据**；重装后 state/config 还在。
- 任何「改生产」的动作（upgrade/uninstall/hold）先跑一次 `--dry-run`，并确认当前 `status` 里没有正在进行的任务（`attention` 非空时先处理）。

## 7. 相关工具

- `tools/remote_doctor.py`：跨机只读体检（Windows / WSL2 / Linux / macOS），输出去敏事实 + findings，`--strict` 有 fail 退出码 1。`--target wsl` 会经 Windows 侧 `wsl.exe` 采样（ssh 落在 cmd 上时 `sh -c` 跑不了）。
- `tools/windows/setup-remote-debug.ps1` / `install-openssh.ps1` / `check-remote-debug.sh`：Windows 侧 SSH 接入的一次性搭建与自检。
- `tools/windows/ssh-bootstrap/`：**离线版 OpenSSH 一键包归档**（`win-ssh-bootstrap v1.1`，自带微软 MSI、自提权、解除 MOTW、多公钥幂等写入、`SUMMARY` 行回传）。无外网/无 winget、批量装机时用它；装完必须跑同目录的 `harden-tailscale.ps1`（把 22 收紧到 tailnet 并关密码登录）——包本身是 `Profile Any` 全开 + 密码登录保留。取回 zip、校验值、与本仓脚本的差异见 [tools/windows/ssh-bootstrap/NOTES.md](../tools/windows/ssh-bootstrap/NOTES.md)。
- `dist/wsl2/wsl/verify.sh`：WSL2 系统级托管（systemd scope + holder）的深度验收，`check` 动作会替你跑。

## 8. 验收/多轮任务测试：前后各存一次状态，收尾按**比对**还原

改状态的测试（挂起/恢复、换 release、重启、拆装单元）在别人机器上跑完后，**"还原"必须还原成
测试开始前的实际状态，不是记忆里更早的状态**。2026-11-05 19:56 的事故就是后者：收尾写 hold 时
理由写着「恢复原挂起」，把 16:45 那次「下班」当成原状态，忽略了她 19:42 已经发过「上班」——那台机器
因此挂起 96 小时、零采集零回复（完整取证见 [docs/HISTORY.md](HISTORY.md) 踩坑 23）。

`tools/state_guard.py` 把这件事变成两条命令：

```bash
# 测试前：存基线（本机用 --local；远端用 --host，走 tools/bridge-remote.sh）
python3 tools/state_guard.py --host <台账里的 tailnet 名> --user PC --save /tmp/pre.json
# 测试后：比对；mode / installed_release / running_release / update_pending 任一变化 → 退出码 3
python3 tools/state_guard.py --host <台账里的 tailnet 名> --user PC --compare /tmp/pre.json
```

- 退出码 `0` 一致、`3` 控制状态变了（**这就是"测试改了生产状态"的证据**）、`2` 目标不可达或用法错误；
- 基线里只有控制字段；`heartbeat_age_seconds`/`installed_at` 这类每秒都在变的字段只展示、不判失败；
- `--compare` 不一致时会直接打印"按基线还原"该执行的那条 `feishu-bridge hold|release`；
- 收尾除了跑比对，还要把 `version`（`installed==running`、`update_pending=false`）和
  `consumer status`（`hold.active` 与 `healthy` 是否与基线一致）的真实输出贴进记录。

## 历史遗留清理（只保留当前版本）

安装器（Windows E 阶段 + WSL finalize）会自动执行下列清理，核对时按这张表看：

| 位置 | 保留 | 清掉 |
|---|---|---|
| `%LOCALAPPDATA%\feishu-bridge\` | `feishu-holder.ps1`、`feishu-holder-launch.ps1`、最新一代 `legacy-*\` | 所有 `backup-*\`、旧 `legacy-*\`、`*.disabled`、`diag*.txt`、`ssh-harden.log`、`task-setup.log`、`watchdog.log`、`resident.log` |
| 计划任务 | `FeishuBridgeBoot` | `FeishuBridgeWatchdog` / `FeishuBridgePostboot` / `FeishuWslHolder`（卸载定义，不只停用） |
| WSL `~` | `feishu-personal-agent/`（当前仓库）、`.feishu-personal-agent/`（数据）、`dsh-workspace/`、`.dsh/`、`.lark-cli/` | `feishu-personal-agent.old-*`、`files/`、`.feishu-bot-lane/`、`bot-bus-*`、`feishu-clean-install.log`、`feishu-*-backup-*.bak` |
| `~/.config/systemd/user/` | 空（WSL 只用系统级 unit） | 同名用户级 unit 定义 + `backup-*` 目录 + `*.wants` 链接 |
| `~/.local/bin/` | `feishu-bridge` 启动器 | 历史占位脚本 |

⚠️ 名字里带控制字符的目录（解压事故留下的 `feishu-personal-agent\r\r`）只有 `ls -b` 才看得见。
**不要**用 `rm -rf ~/feishu-personal-agent*` —— 会把当前仓库一起删掉；用
`find ~ -maxdepth 1 -name $'feishu-personal-agent\r\r' -exec rm -rf {} +` 精确匹配。

## DSH 常驻消费者（唯一的大脑）运维

WSL2 那台默认没有消费者，所以只收不发。装了 DSH 消费者之后，成对的单元是：

| 单元 | 作用 |
|---|---|
| `io.feishu-agent.bridge.service` | bridge 本体：采集 / 详情 / 写事件 / 发送（自己不回答） |
| `io.feishu-agent.dsh-consumer.service` | `dsh-main` 消费者：领事件、跑 DSH、提交回复（`ExecStart` 指向 `current/dsh-consumer.py`，与 macOS 一样从 release 跑） |

常用命令（远程经 `wsl.exe -d Ubuntu -- bash -s` 执行）：

```bash
# 只看状态（不改系统）
sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/install-dsh-consumer.sh --user "$USER" --check
systemctl is-active io.feishu-agent.dsh-consumer
sudo journalctl -u io.feishu-agent.dsh-consumer -n 50 --no-pager

# 改回复超时（`home/external.json` 只有这一项；默认与范围见 [CONTRACTS.md](../CONTRACTS.md)）
feishu-bridge events timeout 600
feishu-bridge events status          # 看 consumer 的 last_seen_at 是否在跳、有没有积压
feishu-bridge doctor --assert        # consumer_offline 是硬失败：积压 >120s 没人领

# 清理过期消息 / 死信（死信事件物理删除；陈旧未完成工作置 dropped；
# 回复已确定不发但状态没收回的置 ignored）。默认只看早于 7 天的，先 --dry-run
feishu-bridge events prune --dry-run
feishu-bridge events prune --older-than-days 1
feishu-bridge events prune --include-done --older-than-days 30   # 连很久以前已完成的事件一起删

# 凭证变动 / 换 release 之后重载消费者（起来就在领，没有"开闸"这步）
sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/install-dsh-consumer.sh --user "$USER" --restart
# 临时不回复：feishu-bridge hold -r "下班"（收件也一起停）；恢复用 feishu-bridge release
```

清理的边界（设计如此，别绕）：`inbox` / `outbox` 行有触发器禁止 `DELETE`，
所以 `events prune` 只删 `events` 里的死信/历史事件，对消息记录只改状态（`dropped` / `ignored`）。
`--include-done` 默认关闭：`events.dedupe_key` 是幂等依据，采集回溯窗口最长 24 小时，
只有明确知道不会再有重复投递时才删已完成事件。

三条容易踩的坑：

1. **platform 原生包丢失** —— `dsh --version` 正常但 `dsh headless` 报
   `No usable native binding found` / `Cannot find module '@deepseek-ai/node-addon-system-linux-x64'`。
   修：`sudo npm install -g --prefix /usr/local --include=optional @deepseek-ai/dsh@<版本>`
   （脚本里的 `DSH_CONSUMER dsh_headless=` 会自检并尝试补装）。
2. **Node 太旧** —— DSH 要求 Node ≥ 20.12；Ubuntu 自带 18 会在 `parseEnv` 处崩。
3. **凭证判定别裸跑 headless** —— `dsh headless` 默认**禁用已保存登录**，直接跑会误报
   `MISSING_CREDENTIAL`。消费者的 overlay（`credentials` / `authorization` / `deepseek-account`
   `disabled: false`）才会恢复 DSH Desktop 的登录——`dsh-consumer.py` 的 overlay 已经这么做了。
   真实缺凭证时才需要放 `DEEPSEEK_API_KEY` 到 `/etc/feishu-bridge/dsh.env`（0600）。
   没凭证时消费者会在 claim 前失败（事件照旧排队，不会丢），`doctor` 会报 `consumer_offline`。
4. **DSH Desktop 与 WSL 共用凭证** —— Windows 侧 `C:\Users\<user>\.dsh\.credentials.yaml`
   与 WSL 侧 `~/.dsh/.credentials.yaml` 可能是同一份内容（sha256 相同），桌面登录一次即可。


## 虚机为什么总是重启

现象是 `journalctl --list-boots` 一天十几条、`vmwp.exe` 反复换 PID。**先定性：不是崩溃。**
虚机每次都是干净关机 —— 关机序列完整走到 `Reached target poweroff.target`，
`systemd-logind: The system will power off now!`，`last -x` 有正常 `shutdown system down` 记录；
全量日志里没有 `kernel panic` / `Oops` / `BUG:` / OOM（`grep` 为空）。所以不存在「程序崩溃把虚机带走」。

真正的两个来源：

| 来源 | 证据 | 现状 |
|---|---|---|
| **WSL 空闲回收**（默认 `vmIdleTimeout=60000`） | 实测：把所有 `wsl.exe` 客户端断开（连 holder 也停），默认配置下虚机在 100 秒内**消失**；把 `vmIdleTimeout` 设为 `-1` 后同样条件下虚机**保持存活**。机制：没有客户端挂着 → 60 秒后 WSL 自己 `poweroff` 这个发行版；下一条 `wsl.exe` 命令再把它启动 → 一次「重启」 | 已用 `vmIdleTimeout=-1` 关掉（见下），holder 本身也长期挂一个 `/bin/sleep infinity` 客户端 |
| **安装器无条件 `wsl --shutdown`** | `install-windows.ps1` 的 A 环境阶段每跑一次就整机重启；今天部署十几次 = 十几次重启 | 已改成**只有 `/etc/wsl.conf` 真的被改写时才** `wsl --shutdown`（`Set-WslConfSystemd` 现在返回 `already`/`changed`/`failed`），常规升级不再动虚机 |

其余零星重启来自显式操作：`quiet-wsl-flash.ps1` 应用配置时、有人在桌面里手工 `wsl --shutdown`、以及 Windows 关机/重启。

⚠️ 注意：`vmIdleTimeout` 在 WSL 3.0.3.0 上**仍然有效**（`strings wslservice.exe` 能查到该键，
上面 60 秒/`-1` 的对照实验也复现了两种行为）。仓库早前「新版 WSL 已移除该键」的说法是错的，已删掉。

### 怎么确认「这次重启是回收还是有人在关」

```bash
# 1) 启动历史与时长
journalctl --list-boots --no-pager | tail -20
last -x | head -20                       # 有 shutdown 记录 = 干净关机

# 2) 关机是哪一类
journalctl --no-pager | grep -E "System is powering down|The system will power off now" | tail
#    有 logind 关机行 = WSL 服务主动 poweroff（回收或 wsl --shutdown）
#    完全没有关机行、下一条 boot 的 FIRST ENTRY 断在中间 = 才是崩溃/断电

# 3) 空闲回收的判定：关机前最后一条业务日志离关机时刻 ~60 秒、期间没有任何 sudo/命令 = 空闲回收
journalctl -b -1 --no-pager -o short-iso | tail -30
```

## 桌面周期性闪窗（WSLg 的 msrdc.exe）

「每隔几分钟闪一下，有时候是整个屏幕大小的黑窗」—— 症状在 Windows 桌面，根因在 WSL，
**不在 bridge 的业务逻辑里**，但 bridge 的保活会把它放大成循环：

```
vmIdleTimeout 默认 60 秒 → 虚机空闲被回收
  → feishu-holder.ps1（每 15 秒轮询）发现 wsl 客户端没了 → 立刻拉起虚机
      → 每次虚机启动，WSLg 都会拉 msrdc.exe 并 show 一个屏幕大小的窗口（~0.4 秒）再隐藏  ← 闪的就是它
```

两条互相独立的触发路径都会拉起 `msrdc.exe`，现场表现一样，修法也一条覆盖：

| 触发 | 证据 | 说明 |
|---|---|---|
| 虚机启动（空闲回收 + 保活拉起） | FlashHook 抓到 `msrdc.exe`，`cls=OPContainerClass` / `IHWindowClass`，`size=1920x1080`（等于屏幕分辨率），父进程链是 `feishu-holder.ps1 的 powershell → wsl.exe` | 间隔 1–11 分钟，正好是「用户感知的每隔几分钟」 |
| WSLg 内部 `weston` 崩溃重启 | `/mnt/wslg/stderr.log` 里每 ~103 秒一条 `weston ... terminated with signal 11`，紧跟一条 `msrdc.exe /wslg /silent ...` | 同一个虚机启动周期内反复发生，pid 连续递增可证没重启虚机 |

它**不是** `cmd.exe`：`OPContainerClass` / `IHWindowClass` 是 WSLg 远程桌面客户端的窗口类，
`msrdc.exe` 在 `C:\Program Files\WSL\` 下。别去计划任务里找它。

修法（这台机器只跑无头服务，两样都不需要）：

```powershell
# 合并 guiApplications=false + vmIdleTimeout=-1 到 %USERPROFILE%\.wslconfig（保留其它键、留备份）
# 然后 wsl --shutdown 让其生效
powershell -NoProfile -ExecutionPolicy Bypass -File dist\wsl2\windows\quiet-wsl-flash.ps1
# 只读 / 回滚 / 只改配置不重启
... quiet-wsl-flash.ps1 -Check
... quiet-wsl-flash.ps1 -Restore
... quiet-wsl-flash.ps1 -NoRestart
# 远程：tools/bridge-remote.sh --host <h> --user PC quiet-wsl [-Check|-Restore]
```

两个键的作用：`guiApplications=false` → WSLg 根本不启动，`msrdc` 不会被拉起；
`vmIdleTimeout=-1` → 虚机不再空闲回收，切断「回收 → 保活拉起 → 闪一下」的循环。

验证：`/mnt/wslg` 下没有 `weston` 进程、`Get-Process msrdc` 为空；开一次进程创建审计
（见下）让虚机重启一次，`msrdc.exe` 事件应为 0。修复前 12 分钟窗口里是 7 次。

### 第三处闪窗：计划任务拉起 launcher 的那个 1129x635 黑窗

msrdc 修好之后还剩一类闪窗：**尺寸 1129x635、标题 `Terminal` 或 `powershell.exe` 路径、类名
`CASCADIA_HOSTING_WINDOW_CLASS`**（Windows 11 把它交给 Windows Terminal 托管）。它不是 WSL 的，
是**计划任务 `FeishuBridgeBoot` 拉起 `feishu-holder-launch.ps1` 时，Task Scheduler 为
`powershell.exe` 创建的控制台窗口**。

为什么 `Settings.Hidden` 没用：那个开关只把任务条目从任务计划程序列表里藏起来，**不隐藏窗口**。
只要任务动作是 `powershell.exe`，每次触发都闪一下。

触发源有三个，第二个就是「为什么一升级就闪」：

| 触发 | 场景 |
|---|---|
| 登录 | 每次登录桌面，任务按 `-AtLogOn` 起 launcher |
| **`install-windows.ps1` 的最后一步 `Start-ScheduledTask`** | **每次重装 / 远程 upgrade / 任何跑 `bridge-upgrade.ps1` 的动作都会闪** |
| 手工 `Start-ScheduledTask` / 排障 | 同上 |

实测证据（FlashHook 日志，2026-11-03）：

```
09:38:44  远程 upgrade 写入 C:\ProgramData\feishu-bridge\bridge-upgrade.ps1
09:38:58  WindowsTerminal.exe  cls=CASCADIA_HOSTING_WINDOW_CLASS  size=1129x635  title='Terminal'   ← 闪
```

修法：把任务动作包一层 `conhost.exe --headless`，控制台照旧存在但**没有窗口**（holder 逻辑一行不改）：

```powershell
# 安装器 2026-11-03+ 自动这么做（含 conhost 不支持时的回退），旧的免手工
$arg = '--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\Users\<你>\AppData\Local\feishu-bridge\feishu-holder-launch.ps1" -Distro "Ubuntu" -LinuxUser "pc"'
Set-ScheduledTask -TaskName 'FeishuBridgeBoot' -Action (New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $arg)
```

验证：`Get-ScheduledTask -TaskName FeishuBridgeBoot | % { $_.Actions }` 看到 `Execute=conhost.exe`、
参数里带 `--headless`；再 `Start-ScheduledTask -TaskName FeishuBridgeBoot`，FlashHook 应为 0 条记录
（实测：改前 1129x635 记录 1 条，改后 0 条，holder 照常拉起）。launcher 里还有一层兜底：
启动后第一件事就是 `ShowWindow(GetConsoleWindow(), 0)`，老系统不支持 `--headless` 时把闪窗从 ~1 秒压到脚本启动前的几十毫秒。

⚠️ 改完别忘：**重装/升级会重新注册任务** —— 2026-11-03 之后的安装器已自带这个动作，更早的包会覆盖回
`powershell.exe`，升级完要复查一次动作。

### 抓「闪窗」的正确方法

轮询快照抓不到 1 秒级的窗口（进程在两次采样之间就消失了），必须用事件驱动：

```powershell
# A. 窗口钩子（能抓到类名/尺寸/标题，最直观）：他们那边留下的 FlashHook.ps1（**仓外临时脚本，需自备**）用 SetWinEventHook(EVENT_OBJECT_SHOW)
powershell -NoProfile -ExecutionPolicy Bypass -File .\FlashHook.ps1 -Minutes 30
#   -> 控制台类窗口进 FlashHook-*.log，其它带标题窗口进 FlashHook-*-all.log（这次就是靠 -all 抓到的）
# 判读：1920x1080 + OPContainerClass/IHWindowClass = msrdc；ConsoleWindowClass + 标题 cmd.exe = 真 cmd

# B. 进程创建审计（含完整命令行，能量化次数）
auditpol /set /subcategory:{0CCE922B-69AE-11D9-BED3-505054503030} /success:enable
# ... 等 8-12 分钟后按父进程聚合 4688
auditpol /set /subcategory:{0CCE922B-69AE-11D9-BED3-505054503030} /success:disable
```

⚠️ 另一类闪窗来自 **DSH Desktop 自身**：`DeepSeek Harness.exe → dsh-subprocess-local/runner.js → powershell.exe → conhost.exe`，
桌面里那个会话每调用一次 shell 工具就闪一个控制台。它与 bridge 无关（4688 里父进程是 `DeepSeek Harness.exe`），
处理方式是停掉那个会话或退出桌面，不是改 `.wslconfig`。

