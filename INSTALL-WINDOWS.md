# Windows 安装与使用指南（形态 A：WSL2 全内）

> **单一事实源分工**：本文件给**手工/逐步**安装与 mac↔win 差异；验收断言、症状表和退出码的权威版在
> [dist/wsl2/README.md](dist/wsl2/README.md)（发行包手册）；裸机从零到能回复的分阶段流程在
> [docs/onboard-windows-dsh-colleague.md](docs/onboard-windows-dsh-colleague.md)。三份重复的部分以 `dist/wsl2/README.md` 为准，
> 改一处请顺手核对其余两处，别再各自演化。
>
> **只想先装前置资源**（WSL2/Ubuntu/systemd/apt 基础包/Node ≥18/lark-cli/Windows OpenSSH Server，**不装 bridge**）：
> 跑 `prereq.cmd` 或 `install-windows.ps1 -Mode Prereq`。阶段顺序只在 [dist/wsl2/README.md](dist/wsl2/README.md) §3.3.1 定义，
> 面向同事的通俗说明在 [docs/windows-prereq-install.md](docs/windows-prereq-install.md)。
>
> **目标机没有外网（或不想依赖网络）**：用离线安装包（00 自检 → 01 安装 → 02 验收，载荷随包），
> 生成方式与离线语义见 [dist/wsl2/README.md](dist/wsl2/README.md) §3.4；`-Mode PrereqCheck` 是它的只读验收模式。

> **先选拓扑，再往下读。** 本仓只支持两条 Windows 形态，唯一权威在 **[WINDOWS-DECISION.md](WINDOWS-DECISION.md)**：
> **形态 A = bridge 装在这台 Windows 的 WSL2 里（默认、推荐）**；**形态 B = 原生 Windows 只做消费者，bridge 在 Mac / 另一台 WSL2**。
> 本文写形态 A；形态 B 见 §8。
>
> **安装前必读** [WINDOWS-DECISION.md](WINDOWS-DECISION.md) §1「不要做的 6 件事」。第 1 条：**遇到失败先报告，不要绕过或自建方案**——先查本文 §9「已知脚本缺陷公告」。
>
> **真机验证状态（2026-11-03 首验，2026-11-05 复核 2.0）**：形态 A 已在真实 Windows 11（WSL 3.0.3.0 / Ubuntu 22.04）上端到端跑通并持续运行 ——
> 安装、升级、bridge + DSH 常驻消费者（12h+ 零重启）、回复投递与飞书读回都验证过；本文标 ⚠需真机 的条目是**当时**未验证的，
> 现在多数已被实测覆盖（闪窗、虚机回收、计划任务、conhost 无窗口 launcher 见 [docs/HISTORY.md](docs/HISTORY.md)）。
> 仍按「装新机要逐条复核」对待：不同 Windows 版本/企业管控策略（MDM、AppLocker）会改变行为。
>
> **2.0 复核（2026-11-05，release 与 Mac 侧同一个 id）**：升级后 `installed_release == running_release`、
> `state.sqlite3` 就地 v3→v6、`events.used_ids` 与 `tasks`/`task_messages` 建出、行数与状态分布不变；
> `verify.sh` 全绿、PS 测试套件全过；FTS5/`trigram` 都可用，`memory.retrieve` 的 `index_mode=fts5` 且中文命中；
> 真实消息闭环与多轮任务（任务账本）都在真机上跑过。细节见 [docs/HISTORY.md](docs/HISTORY.md) §18–§21。
> **升级到 2.0 之后回滚不能只切 release**：库已经到 v6，必须先恢复升级前的 `state.sqlite3` 快照，
> 顺序见 [docs/ops-remote-manage.md](docs/ops-remote-manage.md) §4。

---

## 0. 环境、术语与命令在哪儿执行（3 分钟）

**这是最主要的坑源：Windows 侧和 WSL2 内的命令不能混着敲。** 本文每段都标明执行环境：

| 标记 | 在哪执行 | 怎么进 |
|---|---|---|
| **Windows PowerShell** | Windows 本机（§1 需要**管理员**） | 开始菜单搜 PowerShell → 右键"以管理员身份运行" |
| **WSL2 终端** | WSL2 Ubuntu 内部 | 在 PowerShell 里执行 `wsl -d Ubuntu` |

术语（首次出现即使用，先扫一遍）：

| 术语 | 是什么 | 出问题时看到什么 |
|---|---|---|
| **bridge** | 本仓的桥接服务：lark 收发 + SQLite + sender | `feishu-bridge status`、`~/.feishu-personal-agent/bridge.log` |
| **形态 A / B** | 两条受支持部署拓扑 | [WINDOWS-DECISION.md](WINDOWS-DECISION.md) |
| **消费者 / consumer** | 从 bridge 领事件、处理后交回复的会话或进程 | `feishu-bridge events status`（JSON 的 `consumers` 段） |
| **worker** | bridge 内部的三个线程：`collector`/`details`/`sender` | `status.json` 的 `workers` 有 `false` |
| **消费者离线** | 有 `message` 事件积压、超过 120s 没人领取；`doctor` 的硬失败项 | `doctor --assert` 报 `consumer_offline` |
| **事件超时** | 消费者领了事件却没回复的宽限期，超时记 `dead` + `needs_attention` | `feishu-bridge events timeout <秒>`（默认与范围见 [CONTRACTS.md](CONTRACTS.md)） |
| **事件 / event** | 一条待处理的消息或提醒，带状态与租约 | `feishu-bridge events list` |
| **claim / 租约** | 消费者独占领取事件一段时间；到期可被重投 | `409 ... not leased` |
| **claim_token** | 每次 claim 独立发放，`reply/ack/fail` 必须带上 | 不带就 `409`，带旧的就 `409` |
| **reply / outbox** | 回复正文先写不可变 outbox，再由 sender 发出 | `queued → reply_verified` |
| **网关 / gateway token** | 给非本机消费者用的 HTTP 接口与 Bearer token | `401 unauthorized` |
| **常驻入口 / 会话消费者** | 常驻进程 vs 专用会话 + `/loop` 两种消费方式 | §6 选择表 |
| **skill 根** | DSH 扫描技能目录的位置 | Windows 是 `.dsh\skills` + `.agents\skills`，**不扫 `.codex`** |
| **M1–M5** | 五个验收里程碑（本文用它们卡住每一步） | 见各节「通过标准」 |

---

## 1. 装 WSL2 + Ubuntu + systemd（M1）

> **以下命令在 Windows PowerShell（管理员）中执行。**

```powershell
# 只装前置资源和环境（WSL2/Ubuntu/systemd/apt/Node/lark-cli/OpenSSH；= 发行包里的 prereq.cmd）
powershell -ExecutionPolicy Bypass -File .\dist\wsl2\install-windows.ps1 -Mode Prereq

# 一步到位：前置检查 + 装 bridge（会多要 open_id 与飞书登录，交互更多）
powershell -ExecutionPolicy Bypass -File .\dist\wsl2\install-windows.ps1 `
  -TargetOpenId ou_你自己的id
```

`-Mode Prereq` 只跑前置五步：A 环境 → B 代码落位 → C 依赖（`setup.sh --deps-only`）→ D Windows OpenSSH → E 体检，
**不装 bridge**，装完是「准备好的空机器」（顺序与幂等判定见 [dist/wsl2/README.md](dist/wsl2/README.md) §3.3.1）。

默认 `-Mode Install` 的 A 阶段做：管理员检查（必要时 UAC 提权）→ Windows build（需 2004+/Win11）→ 检查 WSL2，缺失才执行 `wsl --install --no-distribution`（**脚本不执行 `wsl --update`**，老 inbox WSL 需要更新请自己先跑）→ 安装 `Ubuntu`（首次会开窗口建 UNIX 用户；**root 不算已就绪**，脚本会要求建普通用户）→ **写发行版内 `/etc/wsl.conf` 的 `[boot] systemd=true`**（`%USERPROFILE%\.wslconfig` 没有该键，脚本**不写**它；写不进去就打印手工命令并 `exit 3`）→ `wsl --shutdown` → 轮询验证 `systemd --user`（门禁里已补 `XDG_RUNTIME_DIR`）→ 继续 B–F。

不想用脚本时，手动等价步骤（**仍然是 Windows PowerShell**）：

```powershell
wsl --install -d Ubuntu
# 打开一次 Ubuntu，按提示创建 UNIX 用户名/密码；然后回到 PowerShell：

# systemd 是「发行版」配置，写 /etc/wsl.conf（需要 root：wsl -u root 不需要密码）
wsl -d Ubuntu -u root -- bash -lc 'printf "[boot]\nsystemd=true\n" | tee /etc/wsl.conf'
wsl --shutdown

# 自查（期望两行：systemd / USER_SYSTEMD_OK）
wsl -d Ubuntu -- bash -lc 'ps -p 1 -o comm=; systemctl --user show-environment >/dev/null && echo USER_SYSTEMD_OK'
```

> **`.wslconfig` 与 `/etc/wsl.conf` 的区别（一句话）**：`%USERPROFILE%\.wslconfig` 是**全局 WSL 设置**（只认 `[wsl2]`/`[general]`/`[experimental]`，**没有** `[boot] systemd`）；`/etc/wsl.conf` 是**发行版设置**（`[boot]` 在这里）。改 `.wslconfig` 永远治不好 systemd，这正是旧文档让人反复无效操作的原因。
>
> Ubuntu 23.04+ 的 WSL 镜像**默认已开 systemd**：先跑上面那条自查，`ps -p 1` 已经是 `systemd` 就别再改配置。⚠需真机：`.wslconfig [boot]` 在新旧镜像上是否被忽略，未实测；**脚本已删除这个兜底**（不再写 `.wslconfig`），理由见本节末尾的说明。

**M1 通过标准**（Windows PowerShell）：

```powershell
wsl -d Ubuntu -- bash -lc 'ps -p 1 -o comm=; systemctl --user show-environment >/dev/null && echo USER_SYSTEMD_OK'
# 期望两行：systemd / USER_SYSTEMD_OK。只出现第一行 = systemd 没真起来，回本节。
```

跑过 `prereq.cmd` / `-Mode Prereq` 的机器上，M1 的范围更大（该模式的 E 步已逐项断言）：除上面两行，
还要 `lark-cli --version` 有输出、`Get-Service sshd`=Running（未加 `-SkipOpenSsh` 时）。

盘子留 ≥5GB（Node + lark-cli + Cursor CLI + release 快照）；WSL2 磁盘文件在 `%LOCALAPPDATA%\Packages\CanonicalGroupLimited*\LocalState\ext4.vhdx`（扩盘见 `wsl --manage`，⚠需真机）。

### 引导脚本退出码

> 唯一权威表在 **[dist/wsl2/README.md](dist/wsl2/README.md) §3.2**（含 `Install`/`Prereq`/`Check`/`Uninstall`
> 四种模式、每个码的含义与下一步）。本文不再重复该表：同一事实写两处，将来必有一处是错的。
> `install.cmd` / `prereq.cmd` / `check.cmd` 只透传脚本退出码，不自己定义含义。

> **关于 `.wslconfig`**：`[boot] systemd` 不是 `.wslconfig` 的键，写了无效。**脚本原先写 `.wslconfig` 的兜底已经删除**（P0-2）：systemd 只认发行版内 `/etc/wsl.conf` 的 `[boot]`；写失败不会"退一步"，而是打印手工命令并 `exit 3`。
>
> **关于 `wsl --install` 的 3010/1641**：这两个是「功能已装好、需要重启」，**不是失败**。脚本按成功路径处理（提示重启后重跑，退出码 `3010`），真正的失败才会 fallback/`throw`。

---

## 2. 在 WSL2 里装 bridge（M2）

> **以下命令在 WSL2 终端中执行**（先在 PowerShell 里 `wsl -d Ubuntu`）。

顺序是**装 lark-cli → 登录 → 取自己的 open_id → 跑 setup.sh**。旧文档把"登录"写在"装 lark-cli"之前，照做必然 `lark-cli: command not found`。

```bash
sudo apt-get update && sudo apt-get install -y git curl
git clone <本仓库> ~/feishu-personal-agent
cd ~/feishu-personal-agent/dist/wsl2

# ① 装 lark-cli。正确包名是 @larksuite/cli
#    旧脚本把组织段写成 @larksuiteoapi（正确的是 @larksuite），组合出的包在 npm 上 404 —— 这是本仓的已知缺陷，见 §9
sudo npm install -g @larksuite/cli
lark-cli --version                  # 必须有输出（作者机器上为 `lark-cli version 1.0.98`）

# ② 登录。WSL 里没有浏览器：把打印出来的 URL 复制到 Windows 浏览器打开
lark-cli auth login
lark-cli auth status                # 期望 tokenStatus: valid / available: true

# ③ 取你自己的 open_id（= identities.user.openId，ou_ 开头）
lark-cli auth status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["identities"]["user"]["openId"])'

# ④ 首次安装必须显式传入上一步的值
TARGET_OPEN_ID=ou_上面打印的值 bash setup.sh
```

**怎么拿到自己的 open_id（三行 how-to）**：

1. `lark-cli auth status --json` 的输出里，`identities.user.openId` 就是（`ou_` 开头）。
2. 它必须**等于你登录的飞书用户**——bridge 每 60 秒把 `identities.user.openId` 与 `config.json` 的 `target_open_id` 比一次，不相等就 `authenticated=false`、不回复。
3. `TARGET_OPEN_ID` 必须是 `ou_` 开头，否则 `install.py` 直接拒绝（报错文案会重复这段说明）。

**首次安装 vs 升级/重装**（`install.py` 的统一行为，macOS 与 WSL2 相同）：

- **首次安装必须传** `TARGET_OPEN_ID`（全平台一致，没有"默认值"可蹭）。不传会直接失败并打印取值说明。
- **升级/重装不需要再传**：会沿用 `~/.feishu-personal-agent/config.json` 里已有的 `target_open_id`。
- 旧安装里如果存的正是仓库作者的历史默认值，`install.py` 只打 `warning` 不阻断；那条 warning 就该去看 §9 第 7 条。

**正文自称与署名（本人姓名）**：装/升级都**不需要任何身份参数**——回复正文里「你是<本人>的 AI 助手」
和署名里的账号名都会自动等于这台 WSL2 里当前登录的飞书账号姓名（bridge 用 `target_open_id` 校验过的
账号身份，运行时读取；换账号后下一条回复就跟着换，没有安装期写入的人名副本）。取不到账号身份时用中性
自称「你是这个飞书账号本人的 AI 助手」、署名回落成「此回复信息来自账号本人的 AI 助手」，**不会**回退到
任何人名（历史事故见 [docs/HISTORY.md](docs/HISTORY.md) §4）。
判据：`feishu-bridge doctor --assert` 的 `reply_identity`（detail 会写明「来源：本机飞书账号身份」，
署名打印的是渲染后的那一行；取不到账号身份才是 warn）。机制与判据的唯一权威见
[CONTRACTS.md](CONTRACTS.md)「身份与自称」。

**M2 通过标准**：

```bash
command -v lark-cli && lark-cli auth status 2>&1 | head -5
# 期望：有路径输出 + tokenStatus 为 valid（或 available: true）
```

---

## 3. `setup.sh` 每一步做什么、失败怎么办

> **WSL2 终端。** 脚本 `set -euo pipefail`：任一步非零即停，按步号对下表。
> 注意本表只到「用户级 unit 能起来」为止：WSL2 真正的托管是随后的 D 阶段
> `dist/wsl2/wsl/finalize-systemd.sh`（系统级 unit + 停用用户级），见 [dist/wsl2/README.md](dist/wsl2/README.md) §3.3。
>
> 步 1–3（apt 基础包 / Node ≥18 / lark-cli）与下面几步在 `-Mode Prereq` 里由
> `setup.sh --deps-only` 单独跑完（不装 bridge、不登录），所以前置环境不需要读完整张表：
> 先跑 `prereq.cmd`，失败按 [docs/windows-prereq-install.md](docs/windows-prereq-install.md) §五 对。

| 步 | 做什么 | 典型失败 | 怎么办 |
|---|---|---|---|
| 0 | `/mnt` 守卫 + `TARGET_OPEN_ID` + `systemd --user` + 磁盘 | `拒绝安装：源码目录不能位于 /mnt/*` | 回 §2 在 `~/` 重新 clone |
| 0 | 同上 | `首次安装必须显式提供你自己的飞书 open_id` | 回 §2 第 ③ 步取值 |
| 0 | 同上 | 打印 systemd 自查 | 回 §1 写 `/etc/wsl.conf` |
| 1 | apt 依赖（python3/sqlite3/curl/git/npm） | 网络/代理 | 配代理后重跑 |
| 2 | Node ≥18（必要时装 NodeSource 22） | `node -v` 仍 <18 | `sudo apt remove npm` 后重跑；混装 npm 会落到旧 prefix |
| 3 | `sudo npm install -g @larksuite/cli` + `command -v lark-cli` 断言 | `404 Not Found` | 只会是包名/镜像/Node：按脚本打印的三条排查 |
| 4 | 读 `lark-cli auth status --json`，比对 openId | `WARN: TARGET_OPEN_ID != 登录用户的 openId` | 用**你自己**的 open_id 重跑（否则装完 `authenticated=false`） |
| 5 | 确认 `lark-cli` 可执行（桥的唯一硬依赖；**不再装 Cursor**） | `lark-cli 不可执行` | 先 `lark-cli auth login`，再重跑 |
| 6 | `loginctl enable-linger` + `python3 install.py`（release + config + 用户级 systemd unit） | 前置检查/权限 | 按报错文本处理；见 §9 第 7 条 |
| 7 | `feishu-bridge start`（此时是**用户级** unit） | 起不来 | 回 §1 自查 systemd；`systemctl --user status io.feishu-agent.bridge` |
| 8 | **fail-fast 验收**：`authenticated`、worker 全活（`WORKER_NAMES`）、`identity.openId == target_open_id` | `验收未通过（N 项）` | 每条都印了「下一步」，照做后重跑 |

**不要在 `/mnt/c` 下克隆仓库**：脚本会直接拒绝。4 条原因：DrvFs 慢一个量级、POSIX 权限是模拟的、bwrap/landlock 跨盘失真、SQLite WAL 多写者可能损坏且不可回滚。

**M3 通过标准**：

```bash
/home/$USER/.local/bin/feishu-bridge status \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("process_running"),d.get("service_backend"),d.get("workers"))'
# 期望：True systemd {'collector': True, 'details': True, 'sender': True}
```

---

## 4. 服务管理、开机自启

> **WSL2 终端。**

```bash
/home/$USER/.local/bin/feishu-bridge start|stop|restart|status   # 内部走 systemctl --user
/home/$USER/.local/bin/feishu-bridge pause|resume                # 只改 config.json，秒级生效
/home/$USER/.local/bin/feishu-bridge logs                        # bridge.log 最近 80 行
/home/$USER/.local/bin/feishu-bridge doctor                      # JSON：service_backend=systemd、identity、release
systemctl --user status io.feishu-agent.bridge   # unit 名是历史遗留（写死作者标识，身份清偿后改为按安装者派生）
systemctl --user list-units --type=service | grep -i feishu   # 不确定实际 unit 名时用这条
```

开机自启 = **系统级 unit（跑 bridge）+ Windows 计划任务 `FeishuBridgeBoot`（顶住发行版）**：

- bridge 本体：`/etc/systemd/system/io.feishu-agent.bridge.service`，`User=<安装者>`、
  `Restart=always`。它随 WSL 发行版启动而启动，**不依赖任何用户级 systemd 实例**。
- 发行版保活：计划任务只在**登录时**执行 `conhost.exe --headless powershell.exe feishu-holder-launch.ps1`，
  launcher 隐藏窗口后常驻一个 `wsl.exe -d Ubuntu --exec /bin/sleep infinity` 客户端；holder 进程内 15 秒自愈。
  **不要**再建「每 5 分钟重复触发」的任务（每次触发都会弹控制台窗口），也**不要**用
  `schtasks ... /bin/true` + `loginctl enable-linger` 那种写法：WSL 回收会连带销毁用户级 systemd 实例，
  `Linger=yes` 挡不住，结果是「过一会儿就不回消息了」（判据与实测见 [docs/HISTORY.md](docs/HISTORY.md) §2）。

```powershell
# 自查（PowerShell）：任务动作应该是 conhost.exe --headless powershell.exe ...
Get-ScheduledTask FeishuBridgeBoot | Select-Object -ExpandProperty Actions
```

```bash
# 自查（WSL2）：系统级 unit 在跑，而不是 --user
systemctl status io.feishu-agent.bridge --no-pager | head -5   # 不要加 --user
```

---

## 5. 接消费者（它就是这台机器的回复大脑）

> **没有消费者就没人回答**：bridge 只把该回的 @ 写成 `message` 事件，等消费者 claim。消息不会丢（停在 `external_queued`），但 `feishu-bridge doctor --assert` 会以 `consumer_offline` 硬失败（积压超过 120s 没人领取）——这是设计如此，桥没有兜底答复。

> **WSL2 终端。**

```bash
/home/$USER/.local/bin/feishu-bridge events status      # 各 consumer 的 claims/acks、积压、回复超时
/home/$USER/.local/bin/feishu-bridge events timeout 600 # 改回复超时（默认与范围见 CONTRACTS.md）
```

**装包时就该装好两个消费者，不用手工接**：`setup.sh` 第 7.5 步装并启动回话端 `dsh-main` 与干活端 `dsh-worker`，第 8 步断言两个都 `live`；重跑验收用 `sudo bash ~/feishu-personal-agent/dist/wsl2/wsl/check-consumers.sh --user $USER`。手工路径（排障/重装）见下文「让这台机器能回复」（DSH 常驻入口）或 §6。消费者起来后**自动开始领事件**，没有"开闸"这步；要临时不回复就别停消费者，用 `feishu-bridge hold -r "下班"`（收件也一起停）。

---

## 6. Windows 侧接 Agent：三条路径

先选路径，再决定 prompt：

| 路径 | 启动开销 | 有沙箱 | 用哪个 prompt/skill | 什么时候选 |
|---|---|---|---|---|
| **(a) `wsl.exe` 包一层 CLI** | 每次调用几十~几百 ms | ❌ | 本地版 `SESSION-PROMPT.txt`（命令换绝对路径） | 想在原生 Windows 会话里快速跑 |
| **(b) DSH/Codex 装进 WSL2** | 0（常驻） | ✅ `workspace-write` | 直接用本地 skill | **默认推荐** |
| **(c) HTTP 网关** | 0（常驻进程） | ❌（只有网关侧脱敏） | remote skill + `SESSION-PROMPT-WINDOWS.txt` | 非 CLI 脚本、跨机 |

### 6.1 先求值一次，后面所有命令复用（P2-2：别用 `<你>` 占位符，`<` `>` 在 PowerShell 里是重定向符）

> **Windows PowerShell。**

```powershell
$HOME_WSL = (((wsl.exe -d Ubuntu -- bash -lc 'echo $HOME') -join '') -replace "`0", '').Trim()
$BRIDGE   = "$HOME_WSL/.local/bin/feishu-bridge"
$HOME_WSL; $BRIDGE
# 期望：/home/<你的用户名> 与 /home/<你的用户名>/.local/bin/feishu-bridge
```

> `wsl.exe --` 是 **exec 语义：不经过 shell、不展开 `~`、不吃 login PATH**。所以本文 PowerShell 侧的每一处都写绝对路径，不写波浪号路径，也不写裸命令名 `feishu-bridge`。

### 6.2 (a) 用 `wsl.exe` 包一层 CLI（原生 Windows 会话）

> **Windows PowerShell。**

```powershell
& wsl.exe -d Ubuntu -- $BRIDGE events register --consumer win-dsh --kind dsh
& wsl.exe -d Ubuntu -- $BRIDGE events claim --consumer win-dsh --limit 5 --lease 900 --kind message
```

- 专用 DSH 会话 → `/loop 30s` → prompt 用 [`skills/feishu-bridge-consumer/SESSION-PROMPT.txt`](skills/feishu-bridge-consumer/SESSION-PROMPT.txt)，把其中的命令前缀换成 `wsl.exe -d Ubuntu -- /home/<你的用户名>/.local/bin/feishu-bridge ...`（绝对路径）。
- 正文走 stdin（`--body-file -`），不要放命令行参数（`ps` 可见）。
- `wsl.exe` 每次调用有启动开销；高频消费用 6.3。

### 6.3 (b) DSH / Codex 直接装在 WSL2 里（默认推荐，能力最完整）

> **WSL2 终端。** 注意：**必须在 WSL2 里装，不要在 Windows 侧装。**

```bash
# 1) 装 Node 侧 CLI（与 macOS 同一套 npm 包；版本以你环境的 registry 为准）
sudo npm install -g @deepseek-ai/dsh @openai/codex    # 作者环境：dsh 0.2.1-rc.2 / codex 0.165.0
dsh --version; codex --version                        # 都必须有输出

# 2) 凭据（DSH headless 按顺序认三种）
#    a. env DEEPSEEK_API_KEY   b. $DSH_HOME/.credentials.yaml（已 dsh 登录）   c. $DSH_HOME 或 workspace 下的 .env
export DEEPSEEK_API_KEY=...        # 或先登录一次

# 3) 先 --check：不注册、不 claim，只验证适配器可构造 + 打印能力模型
cd ~/feishu-personal-agent
python3 dsh-consumer.py   --consumer dsh-main   --workspace ~/projects/demo --check   # 仓库树里的源码；常驻单元跑的是 current/ 里的 release 副本
python3 codex-consumer.py --consumer codex-main --workspace ~/projects/demo --check

# 4) 正式跑（自己保证同名进程唯一；WSL2 没有 Cursor 消费者服务单元）
python3 dsh-consumer.py   --consumer dsh-main   --workspace ~/projects/demo
python3 codex-consumer.py --consumer codex-main --workspace ~/projects/demo
```

`--check` **期望输出**（作者环境记录，⚠需真机复核）：`Agent adapter is callable; no bridge event was claimed; capabilities={"reply":true,...}`。

- `--workspace` 同时是 cwd 和沙箱写入边界；不设时写入落在私有 run 目录（0600）。
- DSH = bwrap/landlock、Codex = landlock/seccomp；**不要和 macOS/Cursor 的机制互称等价**。
- WSL2 没有 Cursor 消费者服务单元：不要用 `feishu-bridge consumer start|restart`；会话式仍可 `/loop 30s` + 本地 skill。
- 工作空间内容不可信，不要放 `.env`/密钥；写入只指向 ext4 workspace，别指向 `/mnt/c`。

### 6.3.1 装到一半失败：先看退出码，再看日志

`setup.exe` 里的四步（P/S/C/D）各自调用子脚本，子脚本的退出码会原样传出来。
**2 = 前置环境这一层没过**（WSL 组件 / 发行版 / systemd / Linux 普通用户），
其余含义见 `dist/wsl2/install-windows.ps1` 顶部注释。

失败时看这三样，按顺序：

1. 报告 `桌面\feishu-kit-install-*.md`（`setup.exe` 路径）或 `feishu-bridge-install-*.md`（直接跑子脚本）：
   现在会写明「停在哪一步 + 最后一条错误」。
2. 日志 `C:\ProgramData\feishu-kit\install.log`（`setup.exe` 编排层）与
   `C:\ProgramData\feishu-kit\logs\install-windows-*.log`（子脚本逐行输出）。
3. 不知道卡哪一项时，跑**只读体检**（不提权、不改任何文件）。它会先回答「WSL 这一层通不通」
   （发行版在不在、root 能不能跑 bash、目标用户存不存在），再逐项列依赖结论：

```powershell
$kit = 'C:\Program Files\feishu-kit'
powershell -NoProfile -ExecutionPolicy Bypass -File "$kit\offline\repo\dist\wsl2\install-windows.ps1" `
  -Mode PrereqCheck -Distro Ubuntu -PayloadDir "$kit\offline\payload" `
  -ReportPath "$env:USERPROFILE\Desktop\prereqcheck.md"
```

### 6.4 (c) HTTP 网关（任意脚本 / 非 CLI Agent / 跨机）

> **WSL2 终端**（网关跑在 bridge 同一台 WSL2 里）。

```bash
/home/$USER/.local/bin/feishu-bridge events token            # 生成/查看 token（0600）
/home/$USER/.local/bin/feishu-bridge events serve --host 0.0.0.0 --port 8766 --allow-remote
```

> **网关是前台进程，仓库不提供守护**（`events serve` 不注册 unit；`--allow-remote` 是 0.0.0.0 的必需项）。要么自己用 `tmux`/`systemd-run` 保活，要么用下面的 user unit 样例：

```ini
# ~/.config/systemd/user/feishu-bridge-gateway.service   （WSL2 终端里创建）
[Unit]
Description=feishu-bridge HTTP gateway
After=default.target

[Service]
ExecStart=/home/%u/.local/bin/feishu-bridge events serve --host 0.0.0.0 --port 8766 --allow-remote
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
# WSL2 终端
systemctl --user daemon-reload && systemctl --user enable --now feishu-bridge-gateway
systemctl --user status feishu-bridge-gateway
```

Windows 侧（默认 `localhostForwarding=true`）：

```powershell
# Windows PowerShell —— token 用绝对路径读，~ 不会被展开（旧文档这里必然 "No such file"）
$env:BRIDGE_BASE  = 'http://localhost:8766'
$env:BRIDGE_TOKEN = (wsl.exe -d Ubuntu -- cat "$HOME_WSL/.feishu-personal-agent/gateway-token").Trim()
Invoke-RestMethod "$env:BRIDGE_BASE/health"
Invoke-RestMethod -Headers @{Authorization="Bearer $env:BRIDGE_TOKEN"} "$env:BRIDGE_BASE/v1/events/status"
```

**中文正文必须走 UTF-8 字节**：不要用 `Invoke-RestMethod -Body` 直接传字符串（PS 5.1 按非 UTF-8 代码页编码 → 飞书收到 `???`）：

```powershell
# Windows PowerShell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$claim = Invoke-RestMethod -Method Post -Uri "$env:BRIDGE_BASE/v1/events/claim" `
  -Headers @{ Authorization = "Bearer $env:BRIDGE_TOKEN" } -ContentType 'application/json' `
  -Body (@{ consumer='win-dsh'; limit=3; lease_seconds=900; kind='message' } | ConvertTo-Json)

foreach ($ev in $claim.events) {
  # $replyText = 你的回复正文（纯文本、≤1500 字、不含 @、不含密钥原文）
  $bodyFile = Join-Path $env:TEMP ("bridge-reply-" + $ev.id + ".json")
  $json = @{ consumer='win-dsh'; claim_token=$ev.claim_token; body=$replyText } | ConvertTo-Json -Depth 4
  [IO.File]::WriteAllText($bodyFile, $json, (New-Object Text.UTF8Encoding($false)))
  curl.exe -sS -X POST "$env:BRIDGE_BASE/v1/events/$($ev.id)/reply" `
    -H "Authorization: Bearer $env:BRIDGE_TOKEN" -H "Content-Type: application/json; charset=utf-8" `
    --data-binary "@$bodyFile"
}
```

访问不到 `localhost:8766` 时，按顺序试：① 网关是否真在跑、是否 `--host 0.0.0.0`；② `wsl -d Ubuntu -- bash -lc 'hostname -I'` 取 IP 直连；③ **最后手段**才用 portproxy，且**必须显式 `listenaddress=127.0.0.1`**（省略会监听所有地址，等于暴露局域网）：

```powershell
$WSL_IP = (((wsl.exe -d Ubuntu -- bash -lc 'hostname -I') -join '').Trim() -split '\s+')[0]
$WSL_IP
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=8766 connectaddress=$WSL_IP connectport=8766
```

**不要把 8766 暴露到公网。** token 轮换用 `events token --rotate`，**轮换后必须重启 `events serve` 才生效**（运行中的网关保留启动时读入的旧 token）。

### 6.4.1 官方 DSH 桌面端（运营同事的常用入口）

向导装完、`dsh web` 能用，**不代表官方 DSH 桌面端能用**：两者读的是两套东西。

| | `dsh web`（WSL 里） | 官方 DSH 桌面端（Windows 原生，自带 runtime） |
| --- | --- | --- |
| 证书 | WSL 系统 CA + `NODE_EXTRA_CA_CERTS`（`/etc/feishu-bridge/dsh.env`） | Windows 证书库（Chromium 窗口）+ **`NODE_EXTRA_CA_CERTS` 指向导出的 PEM**（node 不读 Windows 证书库） |
| key | `/etc/feishu-bridge/dsh.env`（0600 root） | 用户级环境变量 `DEEPSEEK_API_KEY` + 渠道变量 |
| 渠道/模型 | `~/.dsh/profiles/web/cordis.patch.yml`（WSL） | `%USERPROFILE%\.dsh\profiles\**desktop**\cordis.patch.yml`（Windows，见下） |

**桌面端用的是 `desktop` profile，不是 `web`**。`profiles\desktop\` 是桌面端第一次启动时
自己建的（那份 `cordis.patch.yml` 开头写着「Your patch layer for this dsh profile」），
CLI 也明确拒绝代管它（`dsh --profile desktop --dump-config` → "managed exclusively by the
Electron application"）。真机对照：同一段渠道配置写进 `web` 时，`--profile web --dump-config`
里有 `acme-ai-channel`，`desktop` 那份里一个都没有 —— 写错地方=桌面端静默连不上网关。
（两者 package.json 的 bundle 栈相同，都是 `@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-web-app`，
所以同一段 patch 在两边组合出来的东西一样，区别只在「桌面端读哪个目录」。）

所以 D 阶段在 WSL 装完之后会再补一次 Windows 侧（`Set-WindowsDshAccess`）：

1. 把根证书导出成 PEM → `C:\ProgramData\feishu-kit\certs\gateway-root.crt`，
   并设**用户级** `NODE_EXTRA_CA_CERTS` 指向它；
2. 设用户级 `DEEPSEEK_API_KEY` 与渠道变量（key 只放在环境变量里，不写进任何文件）；
3. 把 AI 配置**并进** `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml`
   （用一对 `# >>> feishu-kit` / `# <<< feishu-kit` 标记包住，重跑只替换这一段，
   不动桌面端自己的 ui-chat / ui-settings 等条目）；机器上还有 `profiles\web`（用过原生
   `dsh web`）就两处都写。两个 profile 目录都不存在（桌面端还没打开过）时，放一份待用文件
   `%USERPROFILE%\.dsh\feishu-kit-cordis.patch.yml`，打开一次桌面端后把它并进
   `profiles\desktop\cordis.patch.yml`。

   **`models:` 里写的是网关给的全套清单，不是只有默认模型那一个。** 清单来自
   `GET <baseURL>/v1/models`（OpenAI 约定，用同一把渠道 key 问），网关服务哪些就写哪些；
   默认模型不在清单里时补在最前面。网关不提供该端点（或 key 无权限）时退回只写默认模型，
   并打一条 `[warn] 取不到 …/v1/models 的模型清单` —— 装了别的网关就会看到这条。
   为什么这么做：清单是**网关的事实**，抄一份到安装程序/构建配置里迟早和网关漂开，
   而漂开的症状是「模型选择器里少几个模型」，没人会想到是安装程序的锅（踩坑 22）。

> **这份文件的正文必须是「顶层 YAML 数组」。** 桌面端刚建好时正文只有一行 `[]`
> （空数组，外加三行注释头）；安装程序把这一行**换成**我们的条目，而不是接在它后面 ——
> `[]` 留着就等于「文档已结束，后面又来一个根节点」，桌面端启动直接
> `failed to parse overlay … YAMLException`，窗口根本不出现（踩坑 21）。手工并
> `feishu-kit-cordis.patch.yml` 那段时同理：**把 `[]` 换成那段**，别接在 `[]` 后面。


判据（真机跑过）：Windows 侧 `node` 直连网关，不带 `NODE_EXTRA_CA_CERTS` 是
`SELF_SIGNED_CERT_IN_CHAIN`，带上就是 `HTTP 401`（证书通了，只差 key）。

续跑（`3010` = 需要重启）：state.json 记下 `reboot_pending`，Inno 登记 `RunOnce\FeishuKitResume`，
重启后自动 `apply.ps1 -ConfigPath <快照> -ResumeState` 接着装。这里有个**必须成立的前提**：
续跑传进来的配置路径就是快照自己（`C:\ProgramData\feishu-kit\install-config.json`），
所以 `Save-ConfigSnapshot` 必须先判「源和目标是不是同一个文件」再决定复制不复制 ——
否则 `Copy-Item` 会抛「无法使用项 … 其自身覆盖该项」，续跑一启动就中止（踩坑 20）。

> **装完必须完全退出桌面端再打开**：环境变量是进程启动时读的，已经开着的那个还是旧环境。
> 代价说清：key 放在用户级环境变量里，意味着**同一个 Windows 账号下的任何程序都能读到**；
> WSL 侧仍然是 0600 root 的更严做法。

### 6.5 从 Windows 起 DSH 的本地 GUI（`dsh web`）

DSH 装在 WSL2 里，**Windows 侧没有 `dsh` 命令**——在 PowerShell 里直接敲 `dsh web` 只会得到「无法识别」。

> **Windows PowerShell / CMD。**

```
wsl.exe -d Ubuntu -u root -- dsh-web
```

- 输出形如 `dsh web: http://127.0.0.1:3080/?token=…`，把这个**带 token** 的地址粘进浏览器。
- **不带 token 会 401**，这是 DSH 的设计，不是装坏了。
- Windows 靠 localhost 转发访问 WSL 的 3080：`127.0.0.1:3080` 可以，WSL 的 `172.x` 地址不行。
- 关掉窗口 = 停掉 GUI。它和 bridge 的 systemd 服务是两回事，**停 GUI 不影响回复**。

`dsh-web` 是安装程序（D 阶段第 6 步）装到 `/usr/local/bin/dsh-web` 的包装，做两件缺一不可的事：

1. `set -a; . /etc/feishu-bridge/dsh.env; set +a` —— 密钥在这个文件里（`0600 root`）；
2. `su <运行用户> -c 'exec dsh web'` —— 切成运行用户，但**不能**用 `su -`。

**为什么不直接 `wsl.exe -d Ubuntu -u demo -- dsh web`**：`demo` 读不到那个 `0600 root` 的 env 文件。少了 `DEEPSEEK_API_KEY` / 渠道 key / `NODE_EXTRA_CA_CERTS` 的 DSH 界面照样能打开，但第一次调模型就 401、或自签网关证书报错——「能开不能用」是最难查的一类故障，所以入口固化成一条命令，不靠人记。（踩坑记录：`docs/HISTORY.md` 16。）

需要临时换参数（例如换端口）时：

```
wsl.exe -d Ubuntu -u root -- dsh-web --port 8080
```

### 6.6 原生 Windows 会话消费者：能力与边界

原生 Windows **不能**跑本仓的 Python 消费者运行时（依赖 POSIX `flock`、`killpg`；`consumer_runtime.require_posix_runtime()` 会在任何 claim 之前直接拒绝）。原生 Windows 上的 DSH / Codex 会话通过 **HTTP 网关 + `skills/feishu-bridge-consumer-remote`** 消费：

- **允许**：回复、检索（读本机文件、搜索、联网）、执行并完成（在会话自己的权限范围内跑命令、改/建文件）。
- **禁止**：删除文件或目录、`rm`/清空/删库/格式化/强制推送等破坏性操作；读取或打印密码、密钥、令牌、`.env`、私钥，回复正文里也不能出现这些原文；委派子代理、使用 MCP。
- **没有本机适配器兜底**：没有 `workspace-write` 沙箱、没有流式工具事件检测、没有 `finalize_answer` 出站脱敏，只有会话自身的工具策略和 skill prompt。
- **协议红线不变**：只走网关 `/v1/events/*`，不直接调 lark-cli、不发飞书消息、不写 bridge 状态文件、不跑 bridge 主机上的 `install.py`/`launchctl`/`feishu-bridge start|stop|restart`。
- **汇报诚实**：只写实际看到和实际做过的事，不把没执行的步骤说成已完成。

**提交前 5 项自查**（`render_reply` 会整条拦掉含密钥的正文，被拦时你只看到一条固定拦截句）：

1. 正文不含密钥/`.env` 原文；2. 正文 ≤1500 字（服务端 6000 截断）；3. 正文不含 `@`；4. 若返回 200 但用户收到固定拦截句 → 看 `body_length` 是否异常小；5. 只写实际做过的事。

**装 remote skill**（Windows 无 symlink 习惯，必须整目录复制）：

```powershell
# Windows PowerShell，在仓库根目录执行
New-Item -ItemType Directory -Force "$env:USERPROFILE\.dsh\skills"    | Out-Null
Copy-Item -Recurse -Force .\skills\feishu-bridge-consumer-remote "$env:USERPROFILE\.dsh\skills\"
New-Item -ItemType Directory -Force "$env:USERPROFILE\.agents\skills" | Out-Null
Copy-Item -Recurse -Force .\skills\feishu-bridge-consumer-remote "$env:USERPROFILE\.agents\skills\"
Test-Path "$env:USERPROFILE\.dsh\skills\feishu-bridge-consumer-remote\SKILL.md"   # 必须 True
```

DSH 扫 `.dsh\skills`（rank 400）与 `.agents\skills`（rank 500）；**`.codex\skills` 只给 Codex CLI 用，DSH 不扫**。⚠需真机：Codex CLI 在 Windows 上是否扫 `.codex\skills` 未验证。

---

## 7. 验收：M4 / M5（断言式，不是人眼读 JSON）

> **WSL2 终端。**

```bash
# M4-⓪ 必须先打开自动回复：新装 config.json 里 sending_enabled=false，
#     不 resume 的话收件照收、但永远不会回——M5 就会"@ 了没反应"。
/home/$USER/.local/bin/feishu-bridge resume
python3 -c 'import json,os
h = os.path.expanduser("~/.feishu-personal-agent")
print("sending_enabled=", json.load(open(h + "/config.json")).get("sending_enabled"))'
# 期望：sending_enabled= True（resume 只改 config.json，秒级生效，不用重启服务）

# M4-① 服务和身份
/home/$USER/.local/bin/feishu-bridge status | python3 -c 'import json,sys
d = json.load(sys.stdin)
print("process_running=", d.get("process_running"), "authenticated=", d.get("authenticated"),
      "service_backend=", d.get("service_backend"), "workers=", d.get("workers"))'
# 期望：process_running= True authenticated= True service_backend= systemd
#       workers= {'collector': True, 'details': True, 'sender': True}

# M4-② 身份必须是本人（不能是仓库作者的历史默认值）
python3 -c 'import json,os
h = os.path.expanduser("~/.feishu-personal-agent")
s = json.load(open(h + "/status.json")); c = json.load(open(h + "/config.json"))
print("openId:", s["identity"]["openId"], "match:", s["identity"]["openId"] == c["target_open_id"])'
# 期望：openId: ou_...  match: True

# M4-③ 消费者已注册（空 claim 必须 count=0，不是报错）
/home/$USER/.local/bin/feishu-bridge events register --consumer win-dsh --kind dsh
/home/$USER/.local/bin/feishu-bridge events claim --consumer win-dsh --limit 1 --lease 60 --kind message
# 期望：JSON，count 为 0 或 1。报"消费者未注册"= 上面 `events register` 没执行；409 not leased = 重新 claim 拿新 claim_token
```

**M5 端到端**：在飞书里 @ 你自己一次 → 消费者 claim → `feishu-bridge logs` 出现 `reply sent` → `reply verified`，且**只回一条**。`status` 的 outbox 走到 `reply_verified`。

**M5 不通过时，先查第一项**：`sending_enabled` 是否为 `true`（回 M4-⓪ 跑 `feishu-bridge resume`）。新装默认是 `false`，此时服务在跑、`authenticated` 也是 `true`、消息也会写成事件，但**不会发出任何回复**——这是最容易误判成"飞书不通"的一项，其它环节都正常。确认 `true` 之后再查消费者是否在领事件（§5 的 `events status`：`consumers[].last_seen_at` 是否在跳、`doctor --assert` 的 `consumer_offline` 是否为 fail）、以及 `feishu-bridge logs` 里的 sender 报错。

---

## 8. 形态 B：原生 Windows 只做消费者

bridge 在**另一台机器**（Mac 或另一台 WSL2），Windows 侧永远不装 `install.py`。完整步骤与传输选择见 [skills/feishu-bridge-consumer-remote/SKILL.md](skills/feishu-bridge-consumer-remote/SKILL.md)；要点：

1. 对端（bridge 所在机器）：`events token` → `events serve`（默认只绑 `127.0.0.1`）→ `events register --consumer win-main --kind other`。
2. Windows：优先 `ssh -N -L 8766:127.0.0.1:8766 <对端>` 隧道，`BRIDGE_TOKEN` 放环境变量；跨机禁止直接 `--allow-remote` 暴露。
3. Windows 会话加载 remote skill，`/loop 30s` 跑 `SESSION-PROMPT-WINDOWS.txt`。
4. 对端把消费者身份挑明：Windows 会话 register 之后就开始领 `message` 事件；停掉该会话就没人回（事件照样排队，`doctor` 的 `consumer_offline` 会报出来）。
5. 中文正文同样走 UTF-8 字节/文件（§6.4 的写法），不要用 `Invoke-RestMethod -Body` 直接传字符串。

---

## 9. 已知脚本缺陷公告（7 条 P0，2026-10-28 已修；旧包看到的按"正确修法"处理）

> 这一节存在的原因：**把脚本 bug 说成"你的环境不行"是这次 Windows 安装卡两天的直接原因。** 下面每条都是本仓自己的缺陷，不是读者的问题。

| # | 旧版现象 | 正确修法（现行代码/文档已按此处理） |
|---|---|---|
| 1 | `setup.sh` 第 4 步 `npm install -g` 用了不存在的组织段 `@larksuiteoapi` + `/lark-cli` → **404**，`set -e` 直接中断 | 包名改成 `@larksuite/cli`（npm 上真实存在，`bin=lark-cli`）。旧包自救：`sudo npm i -g @larksuite/cli` 后重跑 |
| 2 | 不知道 `TARGET_OPEN_ID` 填什么；唯一能算出它的命令要等脚本装完 lark-cli | 先装 lark-cli → `auth login` → `auth status --json` 取 `identities.user.openId` → 再 `TARGET_OPEN_ID=ou_... bash setup.sh` |
| 3 | 按文档顺序：`lark-cli auth login` 先执行，`command not found` | 顺序改为 装 → 登 → 取 → 装（本文 §2 就是这个顺序） |
| 4 | systemd 起不来 → 退出码 3，按提示反复改 `.wslconfig` 永远无效 | systemd 只写在发行版 `/etc/wsl.conf` 的 `[boot]`；**脚本写 `.wslconfig` 的兜底已删除**（该键本来就不存在），写失败直接打印手工命令并 `exit 3`。失败时打印三条自查（含 `XDG_RUNTIME_DIR`） |
| 5 | PowerShell 侧用 `wsl.exe -d Ubuntu -- cat` 读 token 时，路径以波浪号开头 → `No such file` | `wsl.exe --` 是 exec 语义，不展开波浪号。改用绝对路径 `"$HOME_WSL/.feishu-personal-agent/gateway-token"`（§6.1/§6.4） |
| 6 | 5 份文档给 3 套互斥 Windows 拓扑，AI agent 于是自己选型 | 唯一权威 [WINDOWS-DECISION.md](WINDOWS-DECISION.md)，其余文档只留链接 |
| 7 | 安装"成功"但不可用：未登录时 `status` 仍 exit 0，脚本 `|| true` 收尾 | `setup.sh` 末尾 fail-fast：`authenticated==true`、`WORKER_NAMES` 各项全 `true`、`identity.openId == target_open_id`，任一不满足 `exit 1`。旧版 `install.py` 会把作者历史 open_id 当默认值，现在的版本不再有默认值（读到历史值只 warning） |

**遇到上表以外的失败**：把原始报错原样贴出来报告，不要改脚本绕过去（[WINDOWS-DECISION.md](WINDOWS-DECISION.md) §1 第 1 条）。

---

## 10. 排障

| 症状 | 先看什么 | 常见原因 | 跳到 |
|---|---|---|---|
| `lark-cli: command not found` | `command -v lark-cli` | 顺序错（先 login 后装）或 npm prefix 不在 PATH | §2 / §9-3 |
| npm `404 Not Found` | 报错里的包名 | 包名错（`@larksuiteoapi/...`）或私有镜像缺包 | §9-1 |
| 引导脚本 `exit 3` | `ps -p 1 -o comm=` / `cat /etc/wsl.conf` | systemd 没开，或改了 `.wslconfig` | §1 |
| `cat: <波浪号>: No such file` | 命令里有没有波浪号 | PowerShell 侧走 `wsl.exe --`（exec 语义，不展开波浪号） | §6.1/§9-5 |
| `401 unauthorized` | token 是否与对端一致 | token 轮换过 / 网关没重启 / 远程会话没拿到新 token | §5/§6.4 |
| `claim` 恒为 `count=0` | `events status` 的 `consumers[]` 与事件状态 | consumer 没 register，或队列里暂时没有 pending 事件 | §5/§6.2 |
| 中文回复变 `???` | 发送方式 | PS 5.1 用 `-Body` 直接传字符串发中文 | §6.4 |
| `localhost:8766` 连不上 | 网关进程、`--host` | 网关没起 / 只绑回环 / 防火墙 | §6.4 |
| 回复变成固定拦截句 | 网关返回的 `body_length` | 正文命中密钥脱敏 | §6.5 |
| 桌面端双击后没窗口，进程在但隔十几秒重启一次 | `%APPDATA%\@deepseek-ai\dsh-desktop\logs\crash-*-host.log` 里的 `DesktopHostFatalError` | `profiles\desktop\cordis.patch.yml` 不是顶层 YAML 数组（典型：我们的条目被接在 `[]` 后面） | §6.4.1 / `docs/HISTORY.md` 踩坑 21 |
| 模型选择器里只有一个模型（网关明明有好几个） | `grep -A9 'models:' ~/.dsh/profiles/*/cordis.patch.yml`；或直接 `curl -H "Authorization: Bearer $KEY" <baseURL>/v1/models` | 安装程序写的是旧版本（只写默认模型），或安装时取不到 `/v1/models` 打了 `[warn]` 后按默认模型兜底 | §6.4.1 第 3 条 / `docs/HISTORY.md` 踩坑 22 |
| 桌面每隔几分钟闪一次全屏黑窗 | 有没有 `msrdc.exe`；或按 ops 文档用 FlashHook 抓窗口类名 | WSLg 的 `msrdc.exe` 全屏窗口，随 WSL 虚机启动出现；虚机又被 60 秒空闲回收 + holder 15 秒拉起，形成循环 | `dist/wsl2/windows/quiet-wsl-flash.ps1`（写 `guiApplications=false` + `vmIdleTimeout=-1`） |
| `409 ... not leased` | 上一次 claim 的租约 | 租约过期或被别的 consumer 接管：重新 claim 拿新 `claim_token` | §6.2 |
| `401/409 consumer_not_registered` | `events status` 的 `consumers` | 没 register（`events register --consumer NAME --kind dsh`） | §6.2 |
| 数据/性能差 | 仓库与数据在哪 | 放在 `/mnt/c` | §3 |

---

## 11. Windows 与 macOS 差异（9 行，别按 macOS 直觉操作）

| 维度 | macOS | 原生 Windows | WSL2（形态 A） |
|---|---|---|---|
| shell/工具 | `bash` | **`pwsh`**；`bash` 执行器在 win32 被禁用 | `bash`（与 macOS 同构） |
| 沙箱 | Seatbelt | **Windows ACL 受限令牌，强制级别 `partial`** | bwrap/landlock（Codex: landlock/seccomp） |
| 路径 | `~` | `%USERPROFILE%`（`os.homedir()`=USERPROFILE） | `~` |
| 凭据 | Keychain | 平台 keystore（⚠需真机）；无 `chmod 600` 校验，靠 ACL | POSIX 布局 |
| 编码 | UTF-8 | PS 5.1：stdin/stdout OEM 代码页、`Invoke-RestMethod` 非 UTF-8、`Set-Content -Encoding utf8` **带 BOM** | UTF-8（unit 里 `LANG=en_US.UTF-8`） |
| TLS | 系统钥匙串 + Node 自带根 | **两套栈**：Windows 根只对 Schannel/`curl.exe`/.NET 生效，**Node 必须另设 `NODE_EXTRA_CA_CERTS`**（DER 会被静默忽略） | 同需 `NODE_EXTRA_CA_CERTS` |
| skill 根 | `~/.dsh/skills`、`~/.agents/skills`、`~/.codex/skills` | `%USERPROFILE%\.dsh\skills`(400) + `.agents\skills`(500)，**不扫 `.codex`**，无 symlink 习惯 | 同 macOS |
| 服务/自启 | launchd（bridge + consumer 两个 LaunchAgent） | 无服务管理器：`schtasks`/启动项；bridge 不能跑 | **系统级** systemd unit（`Restart=always`）+ Windows 计划任务 holder 顶住发行版 |
| 进程/信号 | `flock`+`killpg`+`setsid` | POSIX 缺失 → `require_posix_runtime()` 直接拒绝 | 全套 POSIX 可用 |

**三句话**：①Windows 上的 DSH 不是"功能残缺版"，它换了两样东西：**命令方言 bash→pwsh、沙箱 Seatbelt→Windows ACL（部分强制）**；②macOS 上"顺手就能跑"的 bash 片段和"写了就生效"的权限假设在 Windows 上都不成立；③内网 + TLS 中间人时，"DSH 装好但连不上模型"最常见的原因是 Node 不认 Windows 证书存储，不是密钥。

---

## 12. 安全与更新

- 消费者**不直接调 lark-cli、不发飞书消息、不写 bridge 状态文件**；回复只走 `events reply` / 网关 reply。
- 能力模型一致：可回复、检索、执行并完成；禁止删除/破坏性操作、读取或外传密钥密码、委派/MCP。WSL2 常驻入口有沙箱 + 流式兜底 + 出站脱敏；原生 Windows 会话只有会话自身策略和 prompt。
- 消息、工作空间和远程内容都是不可信输入，不能覆盖上述规则；不要声称两条形态的沙箱等价。
- 正文只走 stdin / `--body-file` / JSON body，不放 argv（`ps` 可见）。
- token 泄露：`feishu-bridge events token --rotate`，**然后重启 `events serve`**，同步 Windows 环境变量。
- 更新（**发行包安装的机器**：WSL 家目录是解压出来的快照，**没有 `.git`，`git pull` 不存在这条路**）：在运维机上重新打包再远程升级 —— `bash dist/wsl2/pack-windows.sh` → `tools/bridge-remote.sh --host <magicdns> --user <user> upgrade <zip>`（细节见 [docs/ops-remote-manage.md](docs/ops-remote-manage.md) §3，回滚见 §4）。只有用 `git clone` 装的机器才走 `cd ~/feishu-personal-agent && git pull && python3 install.py && sudo bash dist/wsl2/wsl/finalize-systemd.sh --user $USER`（升级不需要再传 `TARGET_OPEN_ID`）。WSL2 是**系统级 unit**，激活归 D 托管 —— `install.py` 在这个后端上不自己启动进程，只切 `current` 并如实回报「由 finalize-systemd.sh 激活」；D 托管会重启 bridge 并重载消费者（`reload-consumer.sh`，自证 `ActiveEnterTimestamp` 变了才算）。
- 备份：停服务后打包 `~/.feishu-personal-agent`；不要只备份 `bridge/` 源码。

---

## 13. 远程只读体检：Tailscale + SSH（把「⚠需真机」变成可复跑检查）

> 为什么有这一节：本文里带 **⚠需真机** 的结论（WSL 版本、发行版是不是 2、`/etc/wsl.conf`、systemd、`XDG_RUNTIME_DIR`、首启 uid、PowerShell 版本、Windows OpenSSH Server 起没起）在 Mac 上静态验证不了，只能读回真机事实。Tailscale 适合干这个：**它只提供网络层**（MagicDNS 名字、设备认证、NAT 穿透），SSH 认证仍由 OpenSSH 负责。

### 13.1 三层各负责什么（别混）

| 层 | 谁提供 | 说明 |
|---|---|---|
| 网络可达 | **Tailscale** | Windows 装 Tailscale，用管理员签的一次性 auth key 入网（不需要同事自己的 Tailscale 账号），得到 `机器名.tailnet.ts.net`；流程见 [onboard-windows-dsh-colleague.md](docs/onboard-windows-dsh-colleague.md) §8.0 |
| SSH 服务端 | **Windows OpenSSH Server**（Windows）/ `tailscale up --ssh`（WSL2/Linux）/ 系统 sshd（macOS） | Windows 原生**不提供** Tailscale SSH server；`tailscale ping` 能通也不代表 `sshd` 在跑 |
| 只读体检 | `tools/remote_doctor.py` | 只用系统 `ssh`，只发查询命令，不写对端 |

### 13.2 Windows 侧准备（管理员 PowerShell）

```powershell
# 1) 装 Tailscale 并用管理员签的 auth key 入网（不需要同事自己的账号），确认名字
#    $env:TS_AUTHKEY = 'tskey-auth-...'  然后：.\tools\windows\setup-remote-debug.ps1 -MacPubKey "<Mac 公钥>"
tailscale status

# 2) Windows 默认只有 SSH 客户端，没有服务端
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Get-Service sshd    # 期望 Running / Automatic

# 3) 账号在 Administrators 组时，OpenSSH 只认这一个文件（不是 ~/.ssh/authorized_keys）
#    C:\ProgramData\ssh\administrators_authorized_keys
#    把 Mac 的 ~/.ssh/id_ed25519.pub 追加进去，并确认权限只给 Administrators+SYSTEM
```

WSL2/Linux 侧（可选，想直连 WSL 时）：`sudo apt install -y openssh-server`，或在该机 `tailscale up --ssh`（Tailscale SSH server 在 Linux/WSL2 上可用）。

> 上面第 2 步在跑过 `prereq.cmd` / `-Mode Prereq` 的机器上已经做过：那个模式的 D 步会装 Windows OpenSSH Server
> （默认防火墙只放行 Tailscale 网段，并停用系统自带的「全来源」规则；`-AllowLanSsh` 才额外放行局域网），
> 但**不写公钥**——「能不能 ssh 进来」仍由本节第 3 步与 [onboard-windows-dsh-colleague.md](docs/onboard-windows-dsh-colleague.md) §8.3 决定。

### 13.3 从 Mac 跑只读体检

```bash
# 先确认网络与 SSH 两层都通
tailscale status
ssh <windows机器名>.tailnet.ts.net "echo ok"

# 只读体检（自动判断对端是 Windows 还是 WSL/Linux）
python3 tools/remote_doctor.py --host <windows机器名>.tailnet.ts.net --target auto

# 生成 Markdown 报告，附在验收记录里
python3 tools/remote_doctor.py --host <windows机器名>.tailnet.ts.net --target windows \
    --markdown reviews/remote-doctor-$(date +%F).md

# 只想审计会执行什么命令（不连接）
python3 tools/remote_doctor.py --host <windows机器名>.tailnet.ts.net --target windows --print-commands
```

输出是 JSON（`findings[]` 里每条带 `status`/`detail`/`hint`），汇总计数：`ok`/`warn`/`fail`/`info`/`unknown`。
退出码：`0` 无 fail、`1` 有 fail、`2` 连不上或用法错误 —— 适合直接塞进验收脚本。

### 13.4 日常远程运维（体检 + 升级，一条命令）

`tools/bridge-remote.sh` 把「走 Tailscale 进 Windows → 进 WSL → 跑 bridge 命令」包成一条命令；
升级也走它（上传发行包 → 解压到 `C:\ProgramData\feishu-bridge\pkg` → `install-windows.ps1 -Mode Install`）：

```bash
tools/bridge-remote.sh --host <windows机器名>.tailnet.ts.net --user <你> status
tools/bridge-remote.sh --host <windows机器名>.tailnet.ts.net --user <你> doctor --assert
tools/bridge-remote.sh --host <windows机器名>.tailnet.ts.net --user <你> check        # + WSL 的 verify.sh
tools/bridge-remote.sh --host <windows机器名>.tailnet.ts.net --user <你> ps-tests     # 两套 PS 测试
tools/bridge-remote.sh --host <windows机器名>.tailnet.ts.net --user <你> upgrade dist/windows/<包>.zip
```

完整说明（含 macOS 宿主、回滚、安全边界）：[docs/ops-remote-manage.md](docs/ops-remote-manage.md)。

### 13.5 它检查什么、不检查什么

| 检查 | 覆盖的「⚠需真机」项 |
|---|---|
| Windows OpenSSH Server 状态/启动类型 | 能否远程取证本身 |
| `wsl --version`、`wsl --list --verbose` 的 VERSION 列 | 是否 WSL2（WSL1 会被判 fail） |
| WSL2 里的 `id -u` / 用户 | 首启是不是 root（`uid=0` 判 fail） |
| `/etc/wsl.conf` 的 `[boot] systemd=true` | 脚本写 wsl.conf 的落点 |
| `systemctl --user show-environment`、`XDG_RUNTIME_DIR`、PID 1 | systemd/XDG 在真机是否可用 |
| `$PSVersionTable` / Windows 版本 | PS 5.1 兼容前提 |
| `python3 --version` / `git --version` / Tailscale 状态 | 安装前置条件 |

**不检查**（必须人工在真机跑，工具明确不碰）：`wsl --install` 的真实退出码 3010/1641、`Start-Process` 的
`ExitCode` 粒度、UAC 提权链路、以及任何 `install.py` / `install-windows.ps1` 的写入行为。工具里**没有**
`--write`/`--fix` 开关，也不接受这类参数。

### 13.6 和形态 B 的关系

Tailscale 让形态 B 更简单：Windows 连 bridge 所在机器的网关，直接按 MagicDNS 名建隧道即可，不需要公网 IP：

```powershell
ssh -N -L 8766:127.0.0.1:8766 <mac机器名>.tailnet.ts.net
```

升级这一步有三条容易踩的坑，都已经在脚本里处理掉（细节与真机实录见
[docs/HISTORY.md](docs/HISTORY.md) §9.0d）：

- 发行包里的中文文件名带 UTF-8 标志位（`dist/wsl2/make-zip.py`；`zip -qr` 不带，Windows 解压会
  报 `Invalid empty pathname`），解包用 Windows 自带的 `System32\tar.exe`，残包直接判失败；
- 升级脚本的 `| tail` 会吃掉退出码 —— 取退出码请 `cmd > log 2>&1; echo $?`；
- 旧安装可能把系统级 unit 写成带人名拼音的名字。D 阶段会按 `ExecStart` 认出它、迁移成
  `io.feishu-agent.bridge` / `io.feishu-agent.dsh-consumer`，并重建消费者单元（失败即让升级失败，
  不静默）。升级后 `verify.sh` 的 `legacy_units` 必须是 `0`、`consumer_release` 必须是 `ok`。

拓扑本身不变（见 [WINDOWS-DECISION.md](WINDOWS-DECISION.md)）：接入层换不掉「形态 A / 形态 B」的选择，也不能把 bridge 装到原生 Windows 上。

## 让这台机器能回复（可选）

默认只收不发（WSL2 内没有可用 agent）。要恢复回复能力，用 DSH 常驻消费者：
从仓库根执行 `dist/wsl2/wsl/install-dsh-consumer.sh`（系统级 unit `io.feishu-agent.dsh-consumer.service`，
入口 `current/dsh-consumer.py`），步骤、凭证与排错见 `dist/wsl2/README.md` 的「让这台机器能回复」一节。
换 release 后由 D 阶段自动重载；只手动重载时用 `bash dist/wsl2/wsl/reload-consumer.sh`（它会自证
`ActiveEnterTimestamp` 变了；见到遗留单元名会明确失败，不会假报"未安装"）。

## 9. as_bot（机器人应用身份）安装

同一个安装器支持两种身份，**一台机器只能选一种**（并存＝同一个群里双答，故直接不支持）。
默认是 `as_user`（你的个人账号）；改用机器人身份：

```bash
# 1) 应用密钥落到文件（0600）。桌面版/lark-cli 已登录同一应用时，可先让密钥落本地：
#    lark-cli config keychain-downgrade
umask 077 && printf '%s' '<APP_SECRET>' > ~/.feishu-app-secret

# 2) 安装（复用现有应用，不新建）
python3 install.py --identity-mode bot   --bot-app-id cli_xxx   --bot-open-id ou_xxx   --owner-open-id ou_xxx   --bot-app-secret-file ~/.feishu-app-secret   --with-dsh-consumer
```

装完会新增**两个**单元（都不需要公网入口）：

| 单元 | 作用 |
|---|---|
| `io.feishu-agent.identity-sidecar` | 长连接收事件 |
| `io.feishu-agent.ingest-gateway` | 只绑 `127.0.0.1:<identity.bot.gateway_port>`（默认 8787），sidecar 把事件 `POST /v1/ingest` 给它 |

安装器同时生成 sidecar 与网关共用的 `<bridge home>/gateway-token`（`0600`，幂等不轮换）。

```bash
systemctl --user is-active io.feishu-agent.identity-sidecar   # 期望 active
systemctl --user is-active io.feishu-agent.ingest-gateway     # 期望 active
feishu-bridge doctor --assert   # 期望 rc=0：authenticated(按 bot 判) / ingest_token / ingest_gateway 全 ok
```

两边缺一个的表现都一样——**机器人不回话**；`doctor` 会直接点名是 token 还是网关，并提示
重跑 `install.py --identity-mode bot`。切换回 as_user 时这两个单元都会被停用并删除。

- 命令通道变成**你与机器人的私聊**（`状态`/`上班`/`下班`/`执行`/`清单`/`撤回`/`记住：`/`help`）；群里发命令不执行
- 切换回个人账号：**先把 `config.json` 里的 `identity.bot` 整块删掉**（`user` 模式带上残留
  `identity.bot.app_id` 会被判成配置矛盾、安装器直接拒绝），再跑
  `python3 install.py --identity-mode user`（它会把 sidecar 与 ingest-gateway 停用并清理资产）
- 机器人应用需要在飞书开放平台开通：`im:message`、`im:message:send_as_bot`、`im:chat:readonly`；
  缺哪个 `doctor` 的 `bot_scopes` 会点名
- sidecar 的运行依赖（`@larksuiteoapi/node-sdk`）由安装器装进
  `<bridge home>/identity-sidecar-app/`（**幂等、跨 release 复用**）；缺 `npm` 或装失败会在
  写单元之前报错——不会出现"单元 active、进程立刻崩"的假成功
