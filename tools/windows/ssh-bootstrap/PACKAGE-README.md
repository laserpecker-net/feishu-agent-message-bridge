# Windows 免密 SSH 一键安装包（v1.1，批量版）

给 Windows 机器安装 OpenSSH Server，并配置**管理员账户**的公钥免密登录。

双击 `install.bat`，除一次 UAC 确认外无需人工输入。

> **本包不依赖任何网络共享。** 目标机器只需要能拿到这个压缩包本身，
> 不需要能访问我们这边任何 NAS。结果默认写回**本机**包目录，
> 并打印一行可复制的 `SUMMARY`。

---

## 包内容

| 文件 | 作用 |
|---|---|
| `install.bat` | 入口：自提权 → 解除文件锁定 → 装 MSI → 调用配置 |
| `configure.ps1` | 启服务、放行防火墙、写公钥、收紧 ACL、自检 |
| `keys.txt` | 要授权的公钥，一行一把，`#` 开头为注释 |
| `settings.txt` | 可选。只在目标机能访问**它们自己的**共享时才用 |
| `OpenSSH-Win64-v10.4.0.0.msi` | 微软官方 MSI |
| `verify-ssh-fleet.sh` | 在 Mac 上批量验证（不需要发给 Windows） |
| `README.md` | 本文件 |

---

## 设计前提（重要）

**登录账户必须是本机 Administrators 组成员。**

Windows OpenSSH 对管理员账户**只读**
`C:\ProgramData\ssh\administrators_authorized_keys`；写进用户目录的
`authorized_keys` 会被**静默忽略**——现象就是"公钥明明装了还问密码"。

本包固定写 `administrators_authorized_keys`，因此它对**所有**管理员账户生效，
与是哪个管理员执行脚本无关。若某台机器必须用非管理员账户登录 SSH，
本包不适用。

---

## 分发方式：飞书

### 一定要发压缩包，不要直接发 .msi / .bat

即时通讯工具常拦截 `.exe / .msi / .bat` 附件。做法：

1. 把整个文件夹压成 `win-ssh-bootstrap-v1.1.zip`
2. 飞书发送该 zip
3. 对方**先解压到本地**（如桌面），**再**双击里面的 `install.bat`

千万不要在压缩软件预览窗口里直接双击 `install.bat`——那样工作目录不确定，
会导致找不到 MSI。

### 文件"被 Internet 锁定"的问题

经飞书/浏览器/解压出来的文件会带 Mark-of-the-Web（"来自 Internet"标记），
可能让 MSI 装不上。`install.bat` 已在提权后**自动执行 `Unblock-File`** 处理，
无需人工右键"解除锁定"。

---

## 三种执行方式

### A. 逐台双击（最省事）

解压到本地 → 双击 `install.bat` → UAC 点"是" → 等窗口出现 `DONE`。

### B. 无人值守（RMM / Intune / 域策略）

```bat
install.bat --nopause
```

退出码：`0` 成功，`1` 失败。

> `--nopause` 要求**调用方本身已提权**（RMM 以 SYSTEM 运行即满足）。
> 普通用户双击时加这个参数无效——自提权会重新拉起一个不带参数的实例。

### C. 共享目录批量（仅当那些机器能访问**它们自己的**共享）

把文件夹放到目标网络内的共享，逐台双击。每台会往同一目录回写
`result-<主机名>.txt`。

如果不是同一目录，用 `settings.txt` 指定：

```
result_dir = \\their-nas\deploy\results
```

**只填目标机器确实能访问的路径。** 填了不可达的路径，脚本会退回本地并
在输出里说明，容易造成"以为收上来了其实没有"。

---

## 结果回收（不依赖网络共享）

每台机器完成后，窗口末尾会打印一行醒目的：

```
SUMMARY host=<机名> user=<用户> arch=AMD64 ip=<内网地址> sshd=Running listen22=True verdict=OK
```

大几十台时建议：

- **让对接人汇总**：每人跑完把这一行发回群里，或截一张末尾的图；
- 或者只收 `verdict=FAIL` 的，成功的不用报；
- 本机也有 `result-<主机名>.txt` 与 `%TEMP%\ssh-bootstrap-*.log` 可追溯。

---

## 验证

### 前提检查：我这台 Mac 能不能连到那些机器？

那些 Windows 在**另一个局域网**。如果两网之间没有路由/VPN/Tailscale 打通，
`verify-ssh-fleet.sh` 从我这里是用不了的——这时**不要**把它当验收手段，
改用上面的 `SUMMARY` 汇总。

### 若网络可达

```bash
./verify-ssh-fleet.sh hosts.txt <WinUser>
```

输出每台的 `TCP22` / `KEY` / `whoami`，汇总 `pass` / `fail`，并生成
`verify-results.csv`。全通过时退出码 0。

用的是 `BatchMode=yes` + `PreferredAuthentications=publickey` + 禁用密码与
keyboard-interactive，**能过就只能是公钥**。

---

## 安全警告（务必读）

Windows OpenSSH 对 Administrators 组成员**给的是提权会话**——没有 UAC 过滤令牌。
`keys.txt` 里每把私钥的持有者，等于拥有那台机器的**本地管理员权限**。

- 私钥按管理员口令的等级保管；
- 收回权限：从 `keys.txt` 删掉对应行后**重跑**（重跑只增不减，
  所以还需要手工到目标机 `administrators_authorized_keys` 里删掉那行）；
- 本包不关闭密码登录，留作公钥失效时的退路。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| 双击秒退/闪一下没了 | 从压缩包预览里直接运行的。先解压到本地再双击 |
| `sshd service still missing after the MSI install` | MSI 失败。手工 `msiexec /i "<MSI>"` 看它自己的报错；常见是安装被策略拦截或已有更高版本 |
| `This machine is ARM64` | x64 MSI 装不了，换 `OpenSSH-ARM64-*.msi` |
| 装完仍要求输密码 | ① 登录账户不是管理员；② 密钥没在 `administrators_authorized_keys`；③ ACL 被改宽 |
| 连接被拒（10061） | sshd 没跑或防火墙没放行，看 `result-*.txt` 的 `listen22` |
| 连接超时 | 防火墙丢包或网络不通。目标机 `netstat -an \| findstr :22` 先看本地是否在听 |
| 结果文件找不到 | 包目录只读，已退回 `%TEMP%`，输出里有实际路径 |

---

## 已验证 / 未验证（诚实标注）

**已实测**（2026-11-02，在一台同事机真机端到端，机名/地址不在仓库里）：

- `install.bat --nopause` 完整流程，退出码 0
- 「sshd 已存在 → 跳过 MSI」分支与 MSI 文件通配匹配
- `configure.ps1` 全流程：服务自启、防火墙、写公钥、ACL、重启、自检
- 幂等性：二次运行 `keys added: 0 (total now: 2)`
- 结果文件与 `SUMMARY` 行输出
- 配置后全新连接回归（ACL 收紧未锁死）
- 从 Mac 用纯公钥 `BatchMode` 登录成功

**未实测**：

- `msiexec /quiet /norestart` 在**全新机器**上的安装路径（测试机已装 OpenSSH，
  走了跳过分支）。**首次批量前请先拿 1 台全新机器试点。**
- UAC 自提权在真实双击场景下的表现（测试是在已提权 SSH 会话里跑的）
- 从飞书下载 → 解压 → 双击 的完整链路
- Windows 10 / Server 各版本

---

## MSI 校验信息

```
文件   : OpenSSH-Win64-v10.4.0.0.msi
大小   : 6889472 字节
sha256 : 788d8643464a01eb63640a2fd746f9ed23d106b5a901a35f88dc8cd40e446ac9
来源   : https://github.com/PowerShell/Win32-OpenSSH/releases
发布者 : Microsoft Corporation（UAC 提权窗口可见）
```

已与 GitHub 官方资产逐字节比对一致（`cmp`）。
