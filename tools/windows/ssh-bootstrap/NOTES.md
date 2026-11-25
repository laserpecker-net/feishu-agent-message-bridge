# win-ssh-bootstrap v1.1：离线 OpenSSH 一键安装包（归档 + 复用说明）

> 本目录是 `win-ssh-bootstrap-v1.1.zip` 的**文本原件归档**，用于以后给新 Windows 装机时直接复用。
> 二进制（`OpenSSH-Win64-v10.4.0.0.msi`）不进 git，见下方「取回与校验」。
>
> 归档日期：2026-11-02 · 来源：`~/Downloads/win-ssh-bootstrap-v1.1.zip`（外部交付；包内 README 记录其
> 2026-11-02 在一台同事机（内网地址见台账，不进仓库）上端到端实测过）
> 本文与 `PACKAGE-README.md`（包内原文）冲突时，**以本文为准**。

---

## 1. 取回与校验

> 直接拿去用：`dist/windows/` 里已经放好 `win-ssh-bootstrap-v1.1.zip`、`harden-tailscale.ps1`、
> `win-ssh-bootstrap-NOTES.md`（本文件副本）三件套，整个目录拷给同事即可（该目录不进 git）。

| 项 | 值 |
|---|---|
| 完整 zip（本地副本，不进 git） | `dist/windows/win-ssh-bootstrap-v1.1.zip`（6.3 MB） |
| zip sha256 | `e326de4a2dd8f02207b500a62b1753b7f33300ce8d67d57ed165a8ab968d1eb1` |
| MSI | `OpenSSH-Win64-v10.4.0.0.msi`，6 889 472 B |
| MSI sha256 | `788d8643464a01eb63640a2fd746f9ed23d106b5a901a35f88dc8cd40e446ac9` |
| MSI 来源 | <https://github.com/PowerShell/Win32-OpenSSH/releases>（包内 README 称已与官方资产 `cmp` 逐字节比对） |

文本原件 sha256（与 zip 内文件一致，已核对）：

```text
1bea88b4462abf32c7fe677efb792876a6b2006e4ca08d68d1992c283c817b98  install.bat
2a37451aa29bc87347ac22e286d011f44e45796382d537edc5ce5e16e45e1d74  configure.ps1
352f301c4ddabaa08263e248f4acef1ad3986bda5f8f98eaa2c86eb2adbeca10  settings.txt
f16bfade2f962ad2972a1f96b7216fc33cb1721c64060ae89b508e864447fe80  PACKAGE-README.md
```

`keys.example.txt` 是**本仓新增的模板**（包内 `keys.txt` 含操作者真实公钥，未入库）。
复原完整包：把 `install.bat` / `configure.ps1` / `settings.txt` / `PACKAGE-README.md` 复制回
`win-ssh-bootstrap-v1.1/`，`keys.example.txt` 改名 `keys.txt` 并填公钥，放入 MSI（核对上面的 sha256），
然后 `zip -qr win-ssh-bootstrap-v1.1.zip win-ssh-bootstrap-v1.1/`（顶层目录名必须与包内一致）。

## 2. 包内实际有什么

| 文件 | 作用 |
|---|---|
| `install.bat` | 入口：`pushd` 自身目录 → UAC 自提权 → `Unblock-File` 解除 MOTW → 找 `OpenSSH-Win64-*.msi` → `msiexec /i /quiet /norestart` → 以 `sc query sshd` 为成功判据 → 调 `configure.ps1` |
| `configure.ps1` | 设 `sshd` 自启 + 启动 → 建/启用防火墙规则 `OpenSSH-Server-In-TCP`（**Profile Any，不限来源**）→ 公钥写入 `C:\ProgramData\ssh\administrators_authorized_keys`（只增不减）→ `icacls /inheritance:r` + Administrators/SYSTEM → 重启 sshd → 自检 → 写 `result-<HOSTNAME>.txt` 并打印 `SUMMARY` 行 |
| `settings.txt` | 可选：`result_dir =` 指定结果目录（默认包目录，其次 `%TEMP%`） |
| `keys.txt` | 一行一把公钥，`#` 注释 |
| `OpenSSH-Win64-v10.4.0.0.msi` | 微软官方 Win32-OpenSSH MSI（x64，**ARM64 不适用**） |

**与包内 README 的不一致**：README 的「包内容」表里有 `verify-ssh-fleet.sh`，但 v1.1 的 zip 里**没有这个文件**
（7 个条目只有 6 个文件 + 目录）。不要按 README 去找它。

**版本号自相矛盾（不影响功能）**：`install.bat` 横幅打 `fleet v1.0`，`configure.ps1` 与 README 标 `v1.1`。
按 zip 名与 configure.ps1 认 v1.1 即可。

## 3. 它做什么 / 不做什么

做：装好 sshd、开机自启、防火墙放行 22、给**所有管理员账户**装公钥免密、收紧密钥文件 ACL、幂等重跑、输出可回传的 `SUMMARY`。

不做（复用要注意）：

1. **不开 Tailscale**，也不把 22 限制到 tailnet —— 规则是 `Profile Any` 且无 `RemoteAddress`，等于对局域网全开。
2. **不关密码登录**（`sshd_config` 保持默认，`PasswordAuthentication` 注释态=允许）——包内 README 说这是「公钥失效时的退路」。
3. **只适用于管理员账户登录 SSH**：Windows OpenSSH 对 Administrators 组成员只读 `administrators_authorized_keys`；
   非管理员账户登录会走用户目录 `authorized_keys`，本包不写那里。
4. 不改端口、不改 `sshd_config`、不生成主机密钥（交给 MSI 默认行为）。

> **重跑会重新打开 22**：`configure.ps1` 每次都会 `Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any`。
> 所以 `harden-tailscale.ps1` 之后如果又重跑了 `install.bat`（比如补一把新公钥），**必须再跑一次 harden**。

## 4. 与本仓现有 SSH 脚本的关系

| | `setup-remote-debug.ps1`（本仓） | `install-openssh.ps1`（本仓） | **win-ssh-bootstrap v1.1**（本文档） |
|---|---|---|---|
| 装 sshd | ✅（**调 `install-openssh.ps1`，不再自带第二份**） | ✅（能力组件，失败退 GitHub MSI，需外网） | ✅（**自带 MSI，完全离线**） |
| Tailscale | ✅ 一起装 + 登录 + 命名 | ❌ | ❌（需另装） |
| 防火墙 | ✅ 由 `install-openssh.ps1` 建规则并按「端口 22 + 来源 Any」停用系统自带全来源规则 | ✅ 只放行 `100.64.0.0/10` + `fd7a:115c:a1e0::/48`（`-AllowLan` 才加 `LocalSubnet`），停不掉就失败 | ⚠ 22 对 Any 全开 |
| 密码登录 | ✅ 同左（配置由 `install-openssh.ps1` 写） | ✅ 写最小 `sshd_config`，`PasswordAuthentication no` | ❌ 保持开启 |
| 公钥 | ✅ 写 Mac 公钥，管理员/普通两处都写 | ❌ 只检查 `administrators_authorized_keys` 在不在 | ✅ 多把公钥，只写管理员文件 |
| 报告 | ✅ 桌面 Markdown + 退出码 0–5 | ❌ | ✅ `SUMMARY` 行 + `result-*.txt` |
| 适用场景 | 已有外网、要一步到位的 Tailscale 远程通道 | 只要 sshd（密钥自己放） | **无外网/无 winget、批量装机、先通 SSH 再补别的** |

结论：**新机器优先用 `setup-remote-debug.ps1`（安全默认更好）；拿不到外网或要批量装时用这个离线包，然后补 hardened 步骤。**

## 5. 复用步骤（形态 A：离线包 + Tailscale 收紧）

```text
① Windows：解压 zip 到本地（不要在压缩软件预览里双击）→ 右键 install.bat 以管理员身份运行
   → 等 `DONE`；抄下 `SUMMARY host=... sshd=Running listen22=True verdict=OK`
② Windows：装 Tailscale 并登录（见 docs/onboard-windows-dsh-colleague.md §8.2）
③ Windows：执行本目录的 harden-tailscale.ps1（管理员）
     powershell -ExecutionPolicy Bypass -File .\harden-tailscale.ps1 -DisablePassword
   → 22 只对 tailnet 放行；密码/键盘交互登录关闭
④ Mac：验证
     ssh -o BatchMode=yes -o PreferredAuthentications=publickey <user>@<magicmDNS> 'echo SSH_OK'
     tools/windows/check-remote-debug.sh --host <magicDNS> --user <user>
```

> `harden-tailscale.ps1` 是**本仓新增**脚本，静态审阅过但**没有 Windows 真机验证**；跑完按第 ④ 步验证，
> 失败时按它的输出回退（它只改防火墙规则与 `sshd_config`，并留 `sshd_config.bak-<时间戳>`）。

## 6. 安全须知（复制给使用的人）

- `keys.txt` 里每把私钥 ≈ 这台机器的本地管理员口令（OpenSSH 管理员会话无 UAC 过滤令牌）。
- 收回权限：从 `keys.txt` 删行**并**到目标机 `C:\ProgramData\ssh\administrators_authorized_keys` 手工删对应行
  —— `configure.ps1` 只增不减，重跑不会删。
- 包内 `keys.txt` 当前含两把真实公钥（注释是各机本地的「用户@主机名」，属于个人信息，`dist/windows/` 因此不进 git）。
  发给别人前换成对方的公钥；不要把带未知公钥的包散出去。
- 装完立刻做第 5 节的 ③④，不要让 22 在全网段长期敞开。

## 7. 复用前 checklist

```text
[ ] 目标机是 x64（ARM64 要换成 OpenSSH-ARM64-*.msi）
[ ] keys.txt 已换成「谁该进来」的公钥；没有多余/过期公钥
[ ] 解压到本地目录再双击（不要在压缩包预览里运行）
[ ] 抄下 SUMMARY 行（host / sshd / listen22 / verdict）
[ ] 已装 Tailscale 并用管理员签的一次性 auth key 入网（不需要同事自己的账号；否则第③步之后 Mac 连不上）
[ ] 跑过 harden-tailscale.ps1 -DisablePassword，且 Mac 侧公钥登录仍成功
[ ] 记录：机器名、MagicDNS 名、登录账户、装包时间、SUMMARY
```

## 8. 已知未验证项（沿用包内 README，未补测）

- `msiexec /quiet` 在**全新机器**上的安装路径（原作者测试机已装 OpenSSH，走了跳过分支）→ **首次批量前先拿 1 台全新机器试点**。
- UAC 自提权在真实双击场景下的表现（原作者是在已提权会话里测的）。
- 飞书下载 → 解压 → 双击 的完整链路（本仓 2026-11-02 装机是远程 SSH 做的，没走这条）。
- Windows 10 / Server 各版本。

## 9. 更新 MSI 的流程

1. 从 <https://github.com/PowerShell/Win32-OpenSSH/releases> 下新版 `OpenSSH-Win64-vX.Y.Z.0.msi`；
2. `shasum -a 256` 与 release 页面/资产比对；
3. 替换 zip 内 MSI，重打包，更新本文第 1 节的版本号/大小/sha256；
4. 抽一台机器重跑一遍 `install.bat`，确认 `verdict=OK`。
