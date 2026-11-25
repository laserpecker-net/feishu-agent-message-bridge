#Requires -Version 5.1
<#
.SYNOPSIS
  把一台 Windows 准备好，让 Mac 上的同事能通过 Tailscale + SSH 远程连进来只读体检 / 调试。

.DESCRIPTION
  只做三件「接入层」的事，不碰飞书 bridge 本身（不跑 install.py、不改 WSL 里的 bridge 状态、不写业务数据）：

    1) 安装并启动 Tailscale，用管理员签发的 auth key 加入同一个 tailnet（$env:TS_AUTHKEY 或 -AuthKey）；
       没给 key 时才退回浏览器登录 —— 那需要同事用**你本人**的账号登录，不是推荐路径；
    2) 启用 Windows OpenSSH Server（原生 Windows 不提供 Tailscale SSH server，必须开这个）；
    3) 把 Mac 侧的公钥写进 OpenSSH 会读的两个位置，然后自检端口是否真的在 listen。

  最后打印一份报告（同时保存成 txt），把报告整段发回给 Mac 侧即可。

  脚本可重复执行：已登录的 Tailscale、已启动的 sshd、已写入的公钥都会跳过。

.PARAMETER MacPubKey
  要授权的公钥（必填）。取 Mac 侧 ~/.ssh/id_ed25519.pub 的内容传进来，脚本不再内置任何人的公钥。

.PARAMETER HostName
  Tailscale 设备名（决定 MagicDNS 名）。默认 = 计算机名清洗后加 -win。

.PARAMETER AuthKey
  可选：tailnet 管理员预签发的 auth key（tskey-auth-...）。填了就不弹浏览器登录，适合无人值守。
  更安全的用法是放进环境变量 TS_AUTHKEY（脚本会自动读取），这样不会出现在命令行参数里。
  注意：auth key 可在 Tailscale 管理后台吊销。

.PARAMETER LoginTimeoutSec
  等浏览器登录的最长秒数，默认 300。

.PARAMETER SshPort
  sshd 端口，默认从 C:\ProgramData\ssh\sshd_config 读，读不到用 22。

.PARAMETER AllowLan
  新建防火墙规则时额外放行本机所在局域网；默认只放行 Tailscale 网段。

.PARAMETER SkipTailscale / SkipSshd
  只做其中一半（例如已经手动装好 Tailscale）。

.PARAMETER ProbeWslBridge
  额外探测 WSL 里有没有 feishu-bridge（会启动默认发行版，只读查询）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup-remote-debug.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup-remote-debug.ps1 -ProbeWslBridge

.NOTES
  退出码：0 全部就绪；1 未提权/UAC 取消；2 Tailscale 未就绪；3 OpenSSH Server 未就绪；
          4 公钥写入失败；5 端口没在 listen（sshd 起了但没监听）。
#>
[CmdletBinding()]
param(
    # 不设默认值：脚本里硬编码操作者的公钥等于把「谁能进来」写死在代码里。
    # 用法：-MacPubKey (Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub" -Raw)
    [string]$MacPubKey = '',
    [string]$HostName = '',
    [string]$AuthKey = '',
    [int]$LoginTimeoutSec = 300,
    [int]$SshPort = 0,
    [switch]$AllowLan,
    [switch]$SkipTailscale,
    [switch]$SkipSshd,
    [switch]$ProbeWslBridge,
    [switch]$NoPause,
    [switch]$NoElevate,
    [string]$OriginalUser = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 支持把 auth key 放在环境变量里（不落命令行参数）：$env:TS_AUTHKEY = 'tskey-auth-...'
if (-not $AuthKey -and $env:TS_AUTHKEY) { $AuthKey = $env:TS_AUTHKEY }

$script:Report = [ordered]@{
    generated_at    = (Get-Date).ToString('s')
    computer_name   = $env:COMPUTERNAME
    whoami          = $null
    login_user      = $null
    lan_ipv4        = @()
    windows         = $null
    powershell      = $PSVersionTable.PSVersion.ToString()
    tailscale       = [ordered]@{}
    sshd            = [ordered]@{}
    authorized_keys = [ordered]@{}
    wsl             = [ordered]@{}
    next_step_for_mac = $null
    problems        = @()
}
$script:Codes = [ordered]@{ tailscale = 0; sshd = 0; key = 0; port = 0 }

# ---------------------------------------------------------------- 输出helpers

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) { Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Err([string]$Text) { Write-Host "  [fail] $Text" -ForegroundColor Red }

function Add-Problem([string]$Text) {
    $script:Report.problems += $Text
}

# ---------------------------------------------------------------- 基础设施

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-ElevatedSelf {
    param([string[]]$Arguments)
    try {
        $child = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $Arguments -PassThru -Wait
    }
    catch {
        Write-Warn '提权被取消或失败（UAC）。本脚本没有安装或修改任何东西。'
        return 1
    }
    if ($null -eq $child -or $null -eq $child.ExitCode) { return 1 }
    return [int]$child.ExitCode
}

function ConvertTo-TsHostName([string]$Raw, [switch]$AppendWin) {
    if (-not $Raw) { return '' }
    $clean = $Raw.ToLowerInvariant() -replace '[^a-z0-9-]', '-'
    $clean = ($clean -replace '-+', '-').Trim('-')
    if ($AppendWin -and $clean -and ($clean -notmatch '-win$')) { $clean = $clean + '-win' }
    if ($clean.Length -gt 55) { $clean = $clean.Substring(0, 55).Trim('-') }
    return $clean
}

# ---------------------------------------------------------------- Tailscale

function Find-TsExe {
    $candidates = @()
    $cmd = Get-Command 'tailscale.exe' -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += 'C:\Program Files\Tailscale\tailscale.exe'
    $candidates += 'C:\Program Files (x86)\Tailscale\tailscale.exe'
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

function Install-Tailscale {
    $exe = Find-TsExe
    if ($exe) {
        Write-Ok "Tailscale 已安装：$exe"
        return $exe
    }

    $installed = $false
    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host '  用 winget 安装 Tailscale.Tailscale ...'
        try {
            & $winget.Source install --id Tailscale.Tailscale --exact --silent `
                --accept-package-agreements --accept-source-agreements 2>&1 |
                ForEach-Object { Write-Host "    $_" }
            $installed = ($LASTEXITCODE -eq 0)
        }
        catch { Write-Warn "winget 失败：$($_.Exception.Message)" }
        if (-not $installed) { Write-Warn "winget 返回 $LASTEXITCODE，改走 MSI。" }
    }
    else {
        Write-Warn '本机没有 winget，改走 MSI 下载安装。'
    }

    if (-not $installed) {
        $arch = 'amd64'
        if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $arch = 'arm64' }
        elseif ($env:PROCESSOR_ARCHITECTURE -eq 'x86') { $arch = 'x86' }
        $url = "https://pkgs.tailscale.com/stable/tailscale-setup-latest-$arch.msi"
        $msi = Join-Path $env:TEMP 'tailscale-setup-latest.msi'
        Write-Host "  下载 $url ..."
        try {
            [Net.ServicePointManager]::SecurityProtocol = `
                [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        }
        catch { }
        Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
        $proc = Start-Process -FilePath 'msiexec.exe' `
            -ArgumentList @('/i', ('"{0}"' -f $msi), '/qn', '/norestart') -PassThru -Wait
        $installed = ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 3010)
        if (-not $installed) { Write-Warn "msiexec 退出码 $($proc.ExitCode)" }
    }

    if (-not $installed) {
        throw 'Tailscale 安装失败：请手动安装 https://tailscale.com/download/windows 后重跑本脚本。'
    }

    # 安装器把 exe 放到 Program Files\Tailscale，等它落盘
    for ($i = 0; $i -lt 20; $i++) {
        $exe = Find-TsExe
        if ($exe) { break }
        Start-Sleep -Seconds 3
    }
    if (-not $exe) { throw 'Tailscale 装完但找不到 tailscale.exe（可能被杀软拦了）。' }
    Write-Ok "Tailscale 已安装：$exe"
    return $exe
}

function Ensure-TsService {
    $svc = Get-Service -Name 'Tailscale' -ErrorAction SilentlyContinue
    if (-not $svc) { throw '找不到 Tailscale 服务：安装可能没完成，重跑本脚本或手动安装。' }
    if ($svc.Status -ne 'Running') {
        Start-Service -Name 'Tailscale'
        Start-Sleep -Seconds 3
    }
    $svc = Get-Service -Name 'Tailscale'
    if ($svc.Status -ne 'Running') { throw "Tailscale 服务没有运行（$($svc.Status)）。" }
    Write-Ok "Tailscale 服务：$($svc.Status)"
}

function Get-TsStatus([string]$Exe) {
    try {
        $raw = & $Exe status --json 2>$null | Out-String
        if (-not $raw -or -not $raw.Trim()) { return $null }
        return ($raw | ConvertFrom-Json)
    }
    catch { return $null }
}

function Get-TsState([string]$Exe) {
    $status = Get-TsStatus $Exe
    if ($null -eq $status) { return $null }
    try { return [string]$status.BackendState } catch { return $null }
}

function Connect-Tailscale {
    param([string]$Exe, [string]$Name, [string]$Key, [int]$TimeoutSec)

    $state = Get-TsState $Exe
    $status = Get-TsStatus $Exe
    $currentName = ''
    if ($status -and $status.Self) { $currentName = [string]$status.Self.HostName }

    if ($state -eq 'Running' -and (-not $Name -or $currentName -eq $Name)) {
        Write-Ok "Tailscale 已登录（设备名 $currentName），跳过登录。"
        return $true
    }

    $tsArgs = @('up')
    if ($Name) { $tsArgs += ('--hostname={0}' -f $Name) }

    if ($Key) {
        Write-Host '  用 auth key 免交互登录 ...'
        & $Exe @tsArgs --auth-key=$Key 2>&1 | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { Write-Warn "tailscale up 退出码 $LASTEXITCODE"; return $false }
        return $true
    }

    Write-Host '  启动 tailscale up：浏览器会弹出 Tailscale 登录页。' -ForegroundColor Cyan
    Write-Host '  **这会要求用同一 tailnet 的账号登录（等于用 Mac 侧本人账号）**；推荐改用一次性 auth key：' -ForegroundColor Yellow
    Write-Host '    $env:TS_AUTHKEY = ''tskey-auth-...''   然后重跑本脚本（见 docs/onboard-windows-dsh-colleague.md §8.0）' -ForegroundColor Yellow

    $outFile = Join-Path $env:TEMP ('tailscale-up-{0}.out' -f [guid]::NewGuid().ToString('N'))
    $errFile = [IO.Path]::ChangeExtension($outFile, '.err')
    $proc = Start-Process -FilePath $Exe -ArgumentList $tsArgs -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $urlShown = $false
    $ok = $false

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (-not $urlShown -and (Test-Path $outFile)) {
            $text = Get-Content -Raw -ErrorAction SilentlyContinue $outFile
            if ($text -and ($text -match 'https://login\.tailscale\.com/\S+')) {
                Write-Host ''
                Write-Host ("  登录链接：{0}" -f $Matches[0]) -ForegroundColor Green
                Write-Host '  浏览器没自动打开就手动打开上面这个链接。' -ForegroundColor Green
                $urlShown = $true
            }
        }
        if ((Get-TsState $Exe) -eq 'Running') { $ok = $true; break }
    }

    if ($proc -and -not $proc.HasExited) {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
    }

    if (-not $ok) {
        $nowState = Get-TsState $Exe
        Write-Warn "等了 $TimeoutSec 秒仍未登录成功（BackendState=$nowState）。"
        if (Test-Path $errFile) {
            $text = (Get-Content -Raw -ErrorAction SilentlyContinue $errFile)
            if ($text) { Write-Warn ("   stderr: " + ($text -replace "`r?`n", ' ')) }
        }
        return $false
    }

    Write-Ok 'Tailscale 已登录（BackendState=Running）。'
    return $true
}

function Get-TsFacts([string]$Exe) {
    $facts = [ordered]@{
        exe            = $Exe
        version        = $null
        service        = $null
        backend_state  = $null
        hostname       = $null
        dns_name       = $null
        ips            = @()
        tailnet_name   = $null
        tailnet_suffix = $null
        magic_dns      = $null
    }

    try { $facts.version = ((& $Exe version 2>$null | Out-String) -split "`r?`n")[0].Trim() } catch { }

    $svc = Get-Service -Name 'Tailscale' -ErrorAction SilentlyContinue
    if ($svc) { $facts.service = "$($svc.Status)/$($svc.StartType)" }

    $status = Get-TsStatus $Exe
    if ($status) {
        try { $facts.backend_state = [string]$status.BackendState } catch { }
        try {
            if ($status.Self) {
                $facts.hostname = [string]$status.Self.HostName
                $dns = [string]$status.Self.DNSName
                if ($dns) { $facts.dns_name = $dns.TrimEnd('.') }
                $facts.ips = @($status.Self.TailscaleIPs)
            }
        }
        catch { }
        try {
            if ($status.CurrentTailnet) {
                $facts.tailnet_name = [string]$status.CurrentTailnet.Name
                $facts.tailnet_suffix = [string]$status.CurrentTailnet.MagicDNSSuffix
                $facts.magic_dns = [bool]$status.CurrentTailnet.MagicDNSEnabled
            }
        }
        catch { }
    }
    return $facts
}

# ---------------------------------------------------------------- OpenSSH Server
#
# sshd 的安装只有一份实现：同目录的 install-openssh.ps1（可选功能 → GitHub MSI 兜底 → 主机密钥 →
# 最小 sshd_config（PasswordAuthentication no）→ 注册并启动服务 → 只放行 tailnet 的规则 + 停用系统
# 自带的「所有来源」规则 → 读 SSH banner 自证 listen）。
#
# 本脚本原先自带一份 Install-SshdServer，两份并行时收敛策略已经各走各的（这里只告警系统自带的全来源
# 规则、那边停用），改一处线上跑的是另一处。已删除本脚本那份，改为调用唯一实现；缺文件就明确失败，
# 不退回自己那份（退回就是又长出第二份）。
function Invoke-OpenSshInstaller([int]$Port) {
    $installer = Join-Path $PSScriptRoot 'install-openssh.ps1'
    if (-not (Test-Path $installer)) {
        throw ("缺少同目录的 install-openssh.ps1：分发时把 tools/windows/ 整个目录一起给" +
               "（本脚本的 sshd 安装就是调它，不再有第二份实现）。")
    }
    if ($Port -le 0) { $Port = Get-SshPort -Override 0 }
    $installerArgs = @{ Port = $Port; NoPause = $true }
    if ($AllowLan) { $installerArgs.AllowLan = $true }
    # 注意：`& script` 的返回值是脚本的**输出**，退出码在 $LASTEXITCODE 里。直接拿返回值判成败，
    # 会因为"输出为空/null"永远判失败（2026-11-03 真机踩到：明明装好了却报退出码为空）。
    & $installer @installerArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw ("install-openssh.ps1 退出码 {0}（细节见桌面 openssh-install-log.txt）。" -f $code)
    }
}

function Get-SshPort([int]$Override) {
    if ($Override -gt 0) { return $Override }
    $cfg = 'C:\ProgramData\ssh\sshd_config'
    if (Test-Path $cfg) {
        try {
            $m = Select-String -Path $cfg -Pattern '^\s*Port\s+(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($m) { return [int]$m.Matches[0].Groups[1].Value }
        }
        catch { }
    }
    return 22
}

function Get-SshdConfigFacts {
    $cfg = 'C:\ProgramData\ssh\sshd_config'
    $facts = [ordered]@{ path = $cfg; exists = $false; pubkey_auth_blocked = $false; password_auth = $null; admin_match = $false }
    if (-not (Test-Path $cfg)) { return $facts }
    $facts.exists = $true
    try {
        $text = Get-Content -Raw -Path $cfg
        if ($text -match '(?m)^\s*PubkeyAuthentication\s+no\s*$') { $facts.pubkey_auth_blocked = $true }
        if ($text -match '(?m)^\s*PasswordAuthentication\s+(yes|no)\s*$') { $facts.password_auth = $Matches[1] }
        if ($text -match '(?m)^\s*Match\s+Group\s+administrators') { $facts.admin_match = $true }
    }
    catch { }
    return $facts
}

function Get-FirewallState([int]$Port) {
    $state = [ordered]@{ tailscale_rule = $null; default_rule = $null }
    $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-Tailscale-In-TCP' -ErrorAction SilentlyContinue
    if ($rule) {
        $addr = ($rule | Get-NetFirewallAddressFilter).RemoteAddress -join ', '
        $state.tailscale_rule = "$($rule.Enabled) remote=$addr"
    }
    $default = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if ($default) {
        $addr = ($default | Get-NetFirewallAddressFilter).RemoteAddress -join ', '
        $state.default_rule = "$($default.Enabled) remote=$addr"
    }
    return $state
}

# ---------------------------------------------------------------- 公钥

function Add-AuthorizedKey {
    param([string]$Path, [string]$Key)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $key = $Key.Trim()
    $exists = $false
    if (Test-Path $Path) {
        $lines = Get-Content -Path $Path -ErrorAction SilentlyContinue
        if ($lines | Where-Object { $_.Trim() -eq $key }) { $exists = $true }
    }
    if (-not $exists) { Add-Content -Path $Path -Value $key -Encoding ASCII }
    if ($exists) { return 'already' }
    return 'added'
}

function Set-AuthorizedKeyAcl {
    param([string]$Path, [string]$Identity)
    # 用 SID 而不是组名：Windows 内置组名随系统语言本地化，"Administrators" 在中文系统上不叫这个名字。
    if ($Identity -eq 'administrators') {
        & icacls.exe $Path /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
    }
    else {
        & icacls.exe $Path /inheritance:r /grant ('*{0}:F' -f $Identity) /grant '*S-1-5-18:F' | Out-Null
    }
}

function Install-AuthorizedKey([string]$Key, [string]$ExtraUser = '') {
    $key = $Key.Trim()
    if ($key -notmatch '^(ssh-|ecdsa-|sk-)') {
        throw '公钥格式不对：应该以 ssh-ed25519 / ssh-rsa / ecdsa- 开头。'
    }

    $result = [ordered]@{}
    # 管理员账号：Windows OpenSSH 只读这个文件（不是 ~/.ssh/authorized_keys）
    $adminFile = 'C:\ProgramData\ssh\administrators_authorized_keys'
    $adminAction = Add-AuthorizedKey -Path $adminFile -Key $key
    Set-AuthorizedKeyAcl -Path $adminFile -Identity 'administrators'
    $result['administrators_authorized_keys'] = "$adminFile ($adminAction)"
    Write-Ok "$adminFile : $adminAction"

    # 普通账号：读用户目录这份
    $userFile = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $userAction = Add-AuthorizedKey -Path $userFile -Key $key
    Set-AuthorizedKeyAcl -Path $userFile -Identity $sid
    $result['user_authorized_keys'] = "$userFile ($userAction)"
    Write-Ok "$userFile : $userAction"

    # UAC 用别的管理员账号提权时，$env:USERPROFILE 是那个管理员的；把你自己的也写一份，否则 ssh 登录时找不到公钥
    if ($ExtraUser -and ($ExtraUser.ToLowerInvariant() -ne $env:USERNAME.ToLowerInvariant())) {
        $extraProfile = Join-Path 'C:\Users' $ExtraUser
        if (Test-Path $extraProfile) {
            $extraFile = Join-Path $extraProfile '.ssh\authorized_keys'
            $extraAction = Add-AuthorizedKey -Path $extraFile -Key $key
            $extraSid = $null
            try {
                $extraSid = (New-Object System.Security.Principal.NTAccount($ExtraUser)).Translate(
                    [System.Security.Principal.SecurityIdentifier]).Value
            }
            catch { }
            if ($extraSid) { Set-AuthorizedKeyAcl -Path $extraFile -Identity $extraSid }
            $result['extra_user_authorized_keys'] = "$extraFile ($extraAction)"
            Write-Ok "$extraFile : $extraAction"
        }
        else { Write-Warn "找不到 $extraProfile，跳过额外用户目录（用 $ExtraUser 登录时可能读不到公钥）。" }
    }

    return $result
}

# ---------------------------------------------------------------- WSL 只读事实

function Get-WslFacts([switch]$Probe) {
    $facts = [ordered]@{}
    if (-not (Get-Command 'wsl.exe' -ErrorAction SilentlyContinue)) {
        $facts.present = $false
        Write-Warn '没有 wsl.exe：这台机器没装 WSL，bridge 不可能在本机 WSL2 里。'
        return $facts
    }

    $facts.present = $true
    try {
        $raw = (& wsl.exe --list --verbose 2>$null | Out-String) -replace "`0", ''
        $lines = @($raw -split "`r?`n" | ForEach-Object { $_.TrimEnd() } | Where-Object { $_.Trim() })
        $facts.distros = $lines -join ' | '
        Write-Ok "WSL: $($facts.distros)"
    }
    catch {
        $facts.distros = "error: $($_.Exception.Message)"
        Write-Warn "读 WSL 列表失败：$($_.Exception.Message)"
    }

    if ($Probe) {
        try {
            $names = @(((& wsl.exe -l -q 2>$null | Out-String) -replace "`0", '') -split "`r?`n" |
                ForEach-Object { $_.Trim() } | Where-Object { $_ })
            if ($names.Count -gt 0) {
                $distro = $names[0]
                $facts.probe_distro = $distro
                # 只读查询：不改 bridge、不 claim 事件
                $cmd = 'id -un; id -u; command -v feishu-bridge || echo no-feishu-bridge; ' +
                       'systemctl --user is-active io.feishu-agent.bridge.service 2>/dev/null || true'
                $probeOut = (& wsl.exe -d $distro -- bash -lc $cmd 2>&1 | Out-String) -replace "`0", ''
                $facts.probe_output = $probeOut.Trim()
                Write-Ok "WSL[$distro] 探测完成：$($facts.probe_output -replace "`r?`n", ' ; ')"
            }
        }
        catch {
            $facts.probe_output = "error: $($_.Exception.Message)"
            Write-Warn "WSL 探测失败：$($_.Exception.Message)"
        }
    }
    return $facts
}

function Test-IsTailscaleIpv4([string]$Ip) {
    # Tailscale 的地址段是 100.64.0.0/10，也就是 100.64.x.x – 100.127.x.x
    if (-not $Ip) { return $false }
    $parts = $Ip.Split('.')
    if ($parts.Count -ne 4 -or $parts[0] -ne '100') { return $false }
    $second = 0
    if (-not [int]::TryParse($parts[1], [ref]$second)) { return $false }
    return ($second -ge 64 -and $second -le 127)
}

function Get-LanIpv4 {
    $ips = @()
    try {
        $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*' -and
                (-not (Test-IsTailscaleIpv4 $_.IPAddress))
            } | Select-Object -ExpandProperty IPAddress)
    }
    catch { }
    return $ips
}

function Test-LocalPort([int]$Port) {
    try {
        $client = New-Object Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $ready = $iar.AsyncWaitHandle.WaitOne(3000)
        if ($ready) { $client.EndConnect($iar) }
        $client.Close()
        return $ready
    }
    catch { return $false }
}

# ---------------------------------------------------------------- 报告

function Get-ReportText {
    $r = $script:Report
    $ts = $r.tailscale
    $sh = $r.sshd
    $lines = @()
    $lines += '# Windows 远程调试准备报告'
    $lines += ''
    $lines += "生成时间: $($r.generated_at)"
    $lines += "计算机名: $($r.computer_name)"
    $lines += "用户(whoami): $($r.whoami)"
    $lines += "登录用户(ssh 用这个): $($r.login_user)"
    $lines += "局域网 IPv4: $($r.lan_ipv4 -join ', ')"
    $lines += "Windows: $($r.windows)"
    $lines += "PowerShell: $($r.powershell)"
    $lines += ''
    $lines += '## Tailscale'
    $lines += "  已安装: $($ts.exe)"
    $lines += "  版本: $($ts.version)"
    $lines += "  服务: $($ts.service)"
    $lines += "  状态: $($ts.backend_state)"
    $lines += "  设备名: $($ts.hostname)"
    $lines += "  MagicDNS: $($ts.dns_name)"
    $lines += "  IP: $($ts.ips -join ', ')"
    $lines += "  tailnet: $($ts.tailnet_name) / $($ts.tailnet_suffix) (MagicDNS=$($ts.magic_dns))"
    $lines += ''
    $lines += '## SSH'
    $lines += "  服务: $($sh.service)  端口: $($sh.port)  listen: $($sh.listening)"
    $lines += "  防火墙-本脚本规则: $($sh.firewall_tailscale_rule)"
    $lines += "  防火墙-系统规则: $($sh.firewall_default_rule)"
    $lines += "  公钥-管理员文件: $($r.authorized_keys.administrators_authorized_keys)"
    $lines += "  公钥-用户文件: $($r.authorized_keys.user_authorized_keys)"
    if ($r.authorized_keys.extra_user_authorized_keys) {
        $lines += "  公钥-额外用户文件: $($r.authorized_keys.extra_user_authorized_keys)"
    }
    if ($sh.exe) { $lines += "  sshd.exe: $($sh.exe)" }
    if ($sh.capability_error) { $lines += "  可选功能安装报错: $($sh.capability_error)" }
    if ($sh.config) {
        $lines += "  sshd_config: $($sh.config.path) (存在=$($sh.config.exists) 公钥登录被禁=$($sh.config.pubkey_auth_blocked) 密码登录=$($sh.config.password_auth) administrators匹配块=$($sh.config.admin_match))"
    }
    $lines += ''
    if ($r.wsl -and $r.wsl.present) {
        $lines += '## WSL'
        $lines += "  发行版: $($r.wsl.distros)"
        if ($r.wsl.probe_output) {
            $lines += "  探测($($r.wsl.probe_distro)): $($r.wsl.probe_output -replace "`r?`n", ' ; ')"
        }
        $lines += ''
    }
    if ($r.problems -and $r.problems.Count -gt 0) {
        $lines += '## 问题'
        foreach ($p in $r.problems) { $lines += "  - $p" }
        $lines += ''
    }
    $lines += '## Mac 侧应该跑的命令'
    $lines += "  $($r.next_step_for_mac)"
    return ($lines -join "`r`n")
}

function Save-Report {
    $text = Get-ReportText
    $desktop = [Environment]::GetFolderPath('Desktop')
    if (-not $desktop -or -not (Test-Path $desktop)) { $desktop = $env:USERPROFILE }
    $path = Join-Path $desktop 'bridge-remote-setup-report.txt'
    try {
        Set-Content -Path $path -Value $text -Encoding UTF8
        Write-Ok "报告已保存：$path"
    }
    catch {
        Write-Warn "写报告失败（$($_.Exception.Message)），下面直接复制控制台内容即可。"
    }
    return $path
}

# ---------------------------------------------------------------- 主流程

function Invoke-Main {
    if (-not (Test-Administrator)) {
        if ($NoElevate) {
            throw '需要管理员权限：请右键「以管理员身份运行」PowerShell，或去掉 -NoElevate 让脚本自己提权。'
        }
        Write-Warn '需要管理员权限，正在弹出 UAC 提权...'
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath), '-NoElevate')
        $arguments += @('-MacPubKey', ('"{0}"' -f $MacPubKey))
        if ($HostName) { $arguments += @('-HostName', ('"{0}"' -f $HostName)) }
        if ($AuthKey) { $arguments += @('-AuthKey', ('"{0}"' -f $AuthKey)) }
        $arguments += @('-LoginTimeoutSec', [string]$LoginTimeoutSec)
        $arguments += @('-OriginalUser', ('"{0}"' -f $env:USERNAME))
        if ($SshPort -gt 0) { $arguments += @('-SshPort', [string]$SshPort) }
        if ($AllowLan) { $arguments += '-AllowLan' }
        if ($SkipTailscale) { $arguments += '-SkipTailscale' }
        if ($SkipSshd) { $arguments += '-SkipSshd' }
        if ($ProbeWslBridge) { $arguments += '-ProbeWslBridge' }
        if ($NoPause) { $arguments += '-NoPause' }
        $code = Invoke-ElevatedSelf -Arguments $arguments
        Write-Host "提权后的子进程已结束，退出码 $code。"
        exit $code
    }

    Write-Step '环境检查'
    try { $script:Report.whoami = (& whoami 2>$null | Out-String).Trim() } catch { }

    $loginUser = $env:USERNAME
    if ($OriginalUser) {
        $loginUser = $OriginalUser
        if ($OriginalUser.ToLowerInvariant() -ne $env:USERNAME.ToLowerInvariant()) {
            Write-Warn "UAC 是用另一个管理员账号（$($env:USERNAME)）提权的，你平时登录的是 $OriginalUser。"
            Write-Warn "公钥会额外写进 C:\Users\$OriginalUser\.ssh\authorized_keys，否则你用 $OriginalUser 登录时读不到公钥。"
        }
    }
    $script:Report.login_user = $loginUser
    try {
        $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        $script:Report.windows = ('{0} build {1}' -f $cv.ProductName, $cv.CurrentBuildNumber)
        Write-Ok "Windows: $($script:Report.windows)"
        if ([int]$cv.CurrentBuildNumber -lt 17763) {
            Write-Warn 'Windows build < 17763：OpenSSH Server 可选功能不可用，需要手动装 OpenSSH 或升级系统。'
        }
    }
    catch { Write-Warn "读 Windows 版本失败：$($_.Exception.Message)" }
    Write-Ok "PowerShell: $($PSVersionTable.PSVersion)"
    Write-Ok "计算机名: $env:COMPUTERNAME / 用户: $env:USERNAME"
    $script:Report.lan_ipv4 = Get-LanIpv4
    if ($script:Report.lan_ipv4.Count -gt 0) { Write-Ok "局域网 IPv4: $($script:Report.lan_ipv4 -join ', ')" }

    $tsName = ConvertTo-TsHostName -Raw $env:COMPUTERNAME -AppendWin
    if ($HostName) { $tsName = ConvertTo-TsHostName -Raw $HostName }
    if (-not $tsName) { $tsName = 'feishu-win' }
    Write-Ok "Tailscale 设备名将设为: $tsName"

    # ---- Tailscale
    if (-not $SkipTailscale) {
        Write-Step 'Tailscale：安装 → 启动服务 → 用 auth key 入同一 tailnet（无 key 才退回浏览器登录）'
        try {
            $exe = Install-Tailscale
            Ensure-TsService
            $loginOk = Connect-Tailscale -Exe $exe -Name $tsName -Key $AuthKey -TimeoutSec $LoginTimeoutSec
            $facts = Get-TsFacts $exe
            $script:Report.tailscale = $facts
            if ($loginOk) {
                Write-Ok "MagicDNS: $($facts.dns_name)   IP: $($facts.ips -join ', ')"
                Write-Ok "tailnet: $($facts.tailnet_name) / $($facts.tailnet_suffix)"
                if ($facts.dns_name -and $facts.tailnet_suffix -and ($facts.dns_name -notlike "*$($facts.tailnet_suffix)")) {
                    Write-Warn 'DNS 名和 tailnet 后缀对不上，检查 MagicDNS 是否开启。'
                }
                if ($facts.magic_dns -eq $false) { Write-Warn '这个 tailnet 关掉了 MagicDNS：直接用 IP 连，别用设备名。' }
            }
            else {
                $script:Codes.tailscale = 2
                Add-Problem 'Tailscale 未完成登录（浏览器登录超时或失败）'
                Write-Err 'Tailscale 未登录：手动点登录链接完成后，重跑本脚本即可（已装好不会重复装）。'
            }
        }
        catch {
            $script:Codes.tailscale = 2
            Add-Problem "Tailscale: $($_.Exception.Message)"
            Write-Err $_.Exception.Message
        }
    }
    else { Write-Warn '按参数跳过 Tailscale。' }

    # ---- SSH
    if (-not $SkipSshd) {
        Write-Step 'OpenSSH Server：安装 → 启动 → 防火墙 → 授权公钥'
        $port = Get-SshPort -Override $SshPort
        try {
            Invoke-OpenSshInstaller -Port $port
            $script:Report.sshd.port = $port
            $cfgFacts = Get-SshdConfigFacts
            $script:Report.sshd.config = $cfgFacts
            if ($cfgFacts.pubkey_auth_blocked) {
                Write-Warn 'sshd_config 里 PubkeyAuthentication 被设成 no：请改成 yes 后重跑本脚本（或 sshd -T 确认）。'
                Add-Problem 'sshd_config 关闭了公钥登录（PubkeyAuthentication no）'
                $script:Codes.key = 4
            }
        }
        catch {
            $script:Codes.sshd = 3
            Add-Problem "OpenSSH: $($_.Exception.Message)"
            Write-Err $_.Exception.Message
        }

        try {
            if (-not $MacPubKey) {
                Write-Err '缺少 -MacPubKey：请传你要授权的公钥（例：-MacPubKey (Get-Content "C:\Users\<你>\.ssh\id_ed25519.pub" -Raw)）'
                return 4
            }
            $script:Report.authorized_keys = Install-AuthorizedKey -Key $MacPubKey -ExtraUser $loginUser
        }
        catch {
            $script:Codes.key = 4
            Add-Problem "公钥: $($_.Exception.Message)"
            Write-Err $_.Exception.Message
        }

        if ($script:Codes.sshd -eq 0) {
            $listening = Test-LocalPort -Port $port
            $svc = Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
            $fw = Get-FirewallState -Port $port
            $script:Report.sshd.service = if ($svc) { "$($svc.Status)/$($svc.StartType)" } else { 'missing' }
            $script:Report.sshd.listening = $listening
            $script:Report.sshd.firewall_tailscale_rule = $fw.tailscale_rule
            $script:Report.sshd.firewall_default_rule = $fw.default_rule
            if ($listening) { Write-Ok "$port/TCP 本地已 listen" }
            else {
                $script:Codes.port = 5
                Add-Problem "sshd 起来了但 $port/TCP 没在 listen"
                Write-Err "$port/TCP 没有监听：sshd 配置可能有错（运行 sshd -T 看）。"
            }
        }
    }
    else { Write-Warn '按参数跳过 OpenSSH Server。' }

    # ---- WSL（只读）
    Write-Step 'WSL 只读事实'
    $script:Report.wsl = Get-WslFacts -Probe:$ProbeWslBridge

    # ---- 结论
    Write-Step '结果'
    $dnsName = $script:Report.tailscale.dns_name
    $ips = @($script:Report.tailscale.ips | Where-Object { $_ })
    $target = if ($dnsName) { $dnsName } elseif ($ips.Count -gt 0) { $ips[0] } else { '<MagicDNS 名>' }
    $command = 'ssh {0}@{1}' -f $loginUser, $target
    $script:Report.next_step_for_mac = $command

    Write-Host '  Mac 侧接下来跑：' -ForegroundColor Cyan
    Write-Host "    $command" -ForegroundColor Green
    Write-Host "    python3 tools/remote_doctor.py --host $target --user $loginUser --target windows" -ForegroundColor Green
    Write-Host ''
    if ($script:Report.lan_ipv4.Count -gt 0) {
        Write-Host '  同一局域网时也可以直连（不依赖 Tailscale）：' -ForegroundColor Cyan
        Write-Host ("    ssh {0}@{1}" -f $loginUser, $script:Report.lan_ipv4[0]) -ForegroundColor Green
    }
    Write-Host '  如果 ssh 报 Permission denied / 用户不存在：' -ForegroundColor Yellow
    Write-Host '    用户名用 whoami 里反斜杠后面那段（微软账号的本地名可能和邮箱不同）。' -ForegroundColor Yellow
    Write-Host '    先把 Mac 侧 ~/.ssh/id_ed25519 的公钥确认就是脚本里那把（改过就重跑本脚本）。' -ForegroundColor Yellow

    Save-Report | Out-Null

    Write-Host ''
    Write-Host '----- 把下面整段复制回传给 Mac 侧 -----' -ForegroundColor Cyan
    $json = $script:Report | ConvertTo-Json -Depth 6
    Write-Host $json
    Write-Host '----- 复制结束 -----' -ForegroundColor Cyan

    $code = 0
    if ($script:Codes.tailscale -ne 0) { $code = 2 }
    elseif ($script:Codes.sshd -ne 0) { $code = 3 }
    elseif ($script:Codes.key -ne 0) { $code = 4 }
    elseif ($script:Codes.port -ne 0) { $code = 5 }
    return $code
}

if ($MyInvocation.InvocationName -ne '.') {
    $exitCode = 0
    try {
        $exitCode = [int](Invoke-Main)
    }
    catch {
        Write-Err $_.Exception.Message
        $exitCode = 1
    }

    if (-not $NoPause) {
        try { Read-Host '按回车关闭窗口（报告已保存在桌面）' | Out-Null } catch { }
    }
    exit $exitCode
}
