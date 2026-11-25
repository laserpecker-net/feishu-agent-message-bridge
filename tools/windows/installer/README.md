# setup.exe（Windows 安装向导）

一个 exe：填配置 → 一步步装 → 完成时告诉你 Mac 上该敲哪条 `ssh`。装完这台机器就等着管理员
SSH 进来部署 bridge，**不在向导里装 bridge**。

## 里面是什么

`feishu-kit.iss`（Inno Setup 6 的界面与流程）+ `apply.ps1`（编排器）+ `build-installer.ps1`（打包）。
安装动作本身没有一行新实现：按顺序调用仓库里的四份既有脚本，谁坏了就是谁坏了。

| 步骤 | 调用 | 干什么 |
| --- | --- | --- |
| P 前置 | `dist/wsl2/install-windows.ps1 -Mode Prereq -SkipOpenSsh` | WSL2、发行版、Linux 用户、apt/Node/lark-cli、体检 |
| S 接入 | `tools/windows/setup-remote-debug.ps1` | Tailscale 入网（auth key）、OpenSSH Server、管理员公钥、端口自检 |
| C 证书 | `tools/windows/install-gateway-cert.ps1` | 网关根证书装到 Windows / WSL / Node 三处（也可单独跑，不需要安装包） |
| D DSH | `dist/wsl2/wsl/install-dsh.sh` | DSH + 渠道配置（provider/baseURL/模型清单/默认模型）+ `/etc/feishu-bridge/dsh.env`（两个变量名同一个 key） |

为什么 `-SkipOpenSsh`：sshd 的开启/防火墙/公钥是 S 阶段（`setup-remote-debug.ps1` → `install-openssh.ps1`）
的职责，P 阶段再开一次就是两套实现。

## 构建

发布构建要两步，**不要**用工作树直接打发布包（`reviews/`、`docs/windows-hosts.csv` 这类
本机文件会被一起装进 exe）：

```bash
# 1) Mac 侧：只打仓库跟踪的文件
git archive --format=zip -o /tmp/feishu-kit-repo.zip HEAD

# 2) Windows 侧（装了 Inno Setup 6）：
powershell -ExecutionPolicy Bypass -File build-installer.ps1 `
  -RepoArchive  C:\Users\demo\feishu-kit-repo.zip `
  -PayloadSource C:\Users\demo\feishu-bridge-win-prereq-20261104\payload `
  -ConfigJson   C:\Users\demo\installer-config.json `
  -OutDir       C:\Users\demo\build-full
```

`-ConfigJson` 是**本机文件、不进 git**（内网地址/渠道名/默认模型都从这里进，仓库里只留 `/D` 占位）：

```json
{ "AppVersion": "1.0.0", "GatewayHost": "<网关IP>", "GatewayBaseUrl": "https://<网关IP>/v1",
  "Provider": "<渠道名>", "ChannelVar": "<KEY 变量名>", "DshModel": "<模型 id>",
  "DshVersion": "0.2.2-rc.1", "DistroName": "Ubuntu", "DefaultLinuxUser": "demo",
  "ApiKeyDefault": "", "AdminPubKeyDefault": "" }
```

`DshModel` 只是**默认模型**：可选模型清单由网关自己给（安装时 `GET <GatewayBaseUrl>/models`，
用向导里填的渠道 key 问），所以这里不需要、也不该维护一份模型列表。网关不提供该端点时
安装程序会 `[warn] 取不到 …/v1/models` 并只写默认模型（`docs/HISTORY.md` 踩坑 22）。

产物：`<OutDir>\out\setup.exe`（`Compression=none`，约 790 MB —— 载荷本身已经
是压缩格式，再压一遍只换来几 MB 却要多等几分钟）。

## 改完 `[Code]` 必须跑冒烟（编译通过 ≠ 能跑）

Inno 的 Pascal 脚本是运行时解释的，类型不匹配、属性用错这类错误 ISCC 不报。本仓踩过一次：
`Page.Add('提示', '默认值')` 编译通过，双击 exe 立刻 `Type Mismatch`（第二参数其实是
`APassword: Boolean`，不是默认值——默认值走 `Values[i]`）。

```powershell
# 拿 5 MB 的 mini 包冒烟（同样的 [Code]，秒级；全量包启动要先把自身复制进临时目录，很慢）
powershell -NoProfile -ExecutionPolicy Bypass -File smoke-test.ps1 `
    -Exe C:\Users\demo\build-mini\out\setup.exe
# → SMOKE_TEST ok=1 reason=reached_validation
```

它用 SYSTEM 计划任务起 `setup.exe /SILENT /LOG=…`（SSH 会话的进程没有提权令牌，直接
`Start-Process` 会卡在看不见的 UAC 上），然后断言：日志建立、日志里没有 `Runtime error` /
`Type Mismatch` / `Internal error`、并且走到了参数校验那条 `Linux 密码至少 8 位`。

## 跑起来会发生什么

1. 三页配置：Linux 用户名/密码 + Tailscale auth key → 网关地址 + 渠道 key + 管理员公钥 → 摘要确认。
2. 复制载荷（`{app}\offline\repo` + `{app}\offline\payload`），然后跑 `apply.ps1`。
3. 日志：`C:\ProgramData\feishu-kit\install.log`；状态：同目录 `state.json`。
4. 需要重启（WSL 首次启用后常见）：向导登记 `RunOnce`，重启后自动接着装，不重头来。
5. 完成页直接显示 `ssh <用户>@<设备名>`；桌面还留一份 `feishu-kit-install-<时间>.md` 报告。

重装/修复：直接再跑一次 exe。已 `done` 的步骤会跳过（`state.json`）；想从头装就删掉
`C:\ProgramData\feishu-kit\state.json`。

## 已知边界

- **DSH 这一步要联网**（`npm i -g @deepseek-ai/dsh`），离线机器上会在 D 阶段失败并给出明确报错。
- **Tailscale 入网必须有 auth key**（`tskey-auth-…`）。留空会退回浏览器登录，那需要设备使用者
  用自己的账号登录，不是推荐路径。
- 签名：当前产物**未签名**，首次运行会被 SmartScreen 拦一次（"更多信息 → 仍要运行"）。
