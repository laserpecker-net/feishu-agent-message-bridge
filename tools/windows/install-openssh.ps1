#Requires -Version 5.1
<#
.SYNOPSIS
    在 Windows 上安装并启动 OpenSSH Server（只做这一步，不碰 bridge）。

.DESCRIPTION
    为什么单独一个脚本：
      - 官方可选功能 Add-WindowsCapability 在家庭版 / WSUS 策略 / 精简镜像上经常失败，
        原脚本会直接把错误吞掉，只留一句「找不到 sshd.exe」。
      - 这里先打印真实错误，再自动退回 GitHub 上的 Win32-OpenSSH MSI。
      - 全程写日志到桌面，控制台就算一闪而过也有据可查。

    -MsiPath（可选）：离线载荷里的 Win32-OpenSSH MSI。给了就先用它（不发网络请求）；
    没给则保持原顺序「Windows 可选功能 → GitHub MSI」。sshd 的安装逻辑只有这一份，
    离线只是「MSI 从哪来」不同，不是第二套实现。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-openssh.ps1
    powershell -ExecutionPolicy Bypass -File .\install-openssh.ps1 -MsiPath D:\payload\windows\OpenSSH-Win64-v10.4.0.0.msi
#>
[CmdletBinding()]
param(
    [int]$Port = 22,
    [switch]$AllowLan,
    [switch]$NoPause,
    [string]$MsiPath = ''
)

$ErrorActionPreference = 'Continue'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-ElevatedSelf([string[]]$ExtraArgs) {
    $argList = @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath)) + $ExtraArgs
    # -Wait：不等子进程就 return，父进程会以 0 退出，自动化无法判断装没装成。
    $child = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -PassThru -Wait
    if ($null -eq $child) { return 1 }
    return [int]$child.ExitCode
}

function Find-SshdExe {
    $winRoot = $env:SystemRoot
    if (-not $winRoot) { $winRoot = 'C:\Windows' }
    $programFiles = $env:ProgramFiles
    if (-not $programFiles) { $programFiles = 'C:\Program Files' }
    foreach ($candidate in @(
            (Join-Path $winRoot 'System32\OpenSSH\sshd.exe'),
            (Join-Path $programFiles 'OpenSSH\sshd.exe'))) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Install-FromMsi([string]$Path) {
    # MSI 安装的**唯一实现**：在线兜底与离线载荷都走这里，只是入参路径来源不同。
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { throw "MSI 文件不存在：$Path" }
    Write-Host "  MSI：$Path"
    # 从网络位置（NAS/共享盘）拷来的 MSI 带 MOTW：不解除会被 SmartScreen/Installer 拦成无提示失败。
    Unblock-File -LiteralPath $Path -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath 'msiexec.exe' `
        -ArgumentList @('/i', ('"{0}"' -f $Path), '/qn', '/norestart') -PassThru -Wait
    $code = -1
    if ($null -ne $proc) { $code = [int]$proc.ExitCode }
    Write-Host "  msiexec 退出码 $code"
    if (-not (Find-SshdExe)) {
        throw "MSI 装完仍找不到 sshd.exe（msiexec=$code）。"
    }
}

function Install-FromGitHub {
    Write-Host '  从 GitHub 取 Win32-OpenSSH（MSI）...' -ForegroundColor Yellow
    try {
        [Net.ServicePointManager]::SecurityProtocol = `
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    catch { }

    $release = $null
    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest' `
            -Headers @{ 'User-Agent' = 'openssh-setup' } -UseBasicParsing -ErrorAction Stop
    }
    catch { Write-Host "  取 release 信息失败：$($_.Exception.Message)" -ForegroundColor Red }

    $asset = $null
    if ($release -and $release.assets) {
        $asset = $release.assets | Where-Object { $_.name -match '^OpenSSH-Win64-v.*\.msi$' } | Select-Object -First 1
    }
    if (-not $asset) { throw '拿不到 Win32-OpenSSH 的 MSI 地址（网络/代理问题）。' }

    $msi = Join-Path $env:TEMP $asset.name
    Write-Host "  $($asset.browser_download_url)"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msi -UseBasicParsing
    Install-FromMsi -Path $msi
}

function Test-FirewallRuleCoversPort($Rule, [int]$Port) {
    # 一条规则是否「以任意来源放行」指定 TCP 端口。判据只看端口/协议，不看名字（名字会变、还可能
    # 是 GUID 或本地化 DisplayName）；端口列表可能是单值、逗号分隔或范围。
    $portFilter = $Rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
    if (-not $portFilter) { return $false }
    if ("$($portFilter.Protocol)" -ne 'TCP') { return $false }
    foreach ($entry in @($portFilter.LocalPort)) {
        $text = "$entry".Trim()
        if ($text -eq 'Any') {
            # 全端口规则不动：本函数的语义是「只收敛 22」，把无关的允许一切规则也关掉会误伤。
            continue
        }
        if ($text -eq "$Port") { return $true }
        $range = [regex]::Match($text, '^(\d+)\s*-\s*(\d+)$')
        if ($range.Success) {
            $low = [int]$range.Groups[1].Value
            $high = [int]$range.Groups[2].Value
            if ($low -le $Port -and $Port -le $high) { return $true }
        }
    }
    return $false
}

# sshd 安装的**唯一实现**：setup-remote-debug.ps1 的 sshd 阶段直接调本脚本（Invoke-OpenSshInstaller），
# 不再自带第二份。收敛策略只在这里一处：按「端口 22 + 来源 Any」停用系统自带的全来源规则，
# 停不掉就让安装失败（静默降级不是修复）。
function Install-SshdServer {
    $sshdExe = Find-SshdExe

    # 离线优先：给了 -MsiPath 就先用它（断网机器上「可选功能」和 GitHub 都到不了）。
    # 文件不存在是参数错，直接抛错而不是静默改走联网——那会让「离线装完」变成假话。
    if (-not $sshdExe -and $MsiPath) {
        Write-Step '离线 MSI 安装 Win32-OpenSSH'
        if (-not (Test-Path -LiteralPath $MsiPath)) {
            throw "指定的 -MsiPath 不存在：$MsiPath（检查载荷是否完整；不要静默改走联网）"
        }
        Install-FromMsi -Path $MsiPath
        $sshdExe = Find-SshdExe
    }

    if (-not $sshdExe) {
        Write-Step '试 Windows 自带可选功能（OpenSSH.Server）'
        $capability = $null
        try {
            $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
        }
        catch { Write-Host "  查询可选功能失败：$($_.Exception.Message)" -ForegroundColor Red }

        if ($capability) {
            Write-Host "  状态：$($capability.State)"
            if ($capability.State -ne 'Installed') {
                try {
                    Add-WindowsCapability -Online -Name $capability.Name -ErrorAction Stop | Out-Null
                    Write-Host '  可选功能安装成功' -ForegroundColor Green
                }
                catch {
                    Write-Host "  可选功能安装失败（走兜底）：$($_.Exception.Message)" -ForegroundColor Red
                }
            }
        }
        else {
            Write-Host '  这台机器没有 OpenSSH.Server 可选功能（家庭版/精简镜像常见）' -ForegroundColor Yellow
        }
        $sshdExe = Find-SshdExe
    }

    if (-not $sshdExe) {
        Write-Step '兜底：安装 Win32-OpenSSH'
        Install-FromGitHub
        $sshdExe = Find-SshdExe
    }
    if (-not $sshdExe) { throw '两处都找不到 sshd.exe，安装未成功。' }
    Write-Host "  sshd.exe = $sshdExe" -ForegroundColor Green

    Write-Step '主机密钥 / sshd_config'
    $programData = $env:ProgramData
    if (-not $programData) { $programData = 'C:\ProgramData' }
    $sshDir = Join-Path $programData 'ssh'
    if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }

    $keygen = Join-Path (Split-Path $sshdExe) 'ssh-keygen.exe'
    if (Test-Path $keygen) { & $keygen -A 2>&1 | Out-Null }

    $configPath = Join-Path $sshDir 'sshd_config'
    if (-not (Test-Path $configPath)) {
        $minimal = @(
            "Port $Port",
            'PubkeyAuthentication yes',
            # 只允许公钥（与本机实测姿态一致）：密码登录一律关掉。
            'PasswordAuthentication no',
            'AuthorizedKeysFile .ssh/authorized_keys',
            'Match Group administrators',
            '       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys'
        ) -join "`r`n"
        Set-Content -Path $configPath -Value $minimal -Encoding ASCII
        Write-Host "  已写入最小配置：$configPath" -ForegroundColor Green
    }
    else {
        Write-Host "  已有配置：$configPath"
    }

    Write-Step '注册并启动 sshd 服务'
    if (-not (Get-Service -Name 'sshd' -ErrorAction SilentlyContinue)) {
        $installScript = Join-Path (Split-Path $sshdExe) 'install-sshd.ps1'
        if (Test-Path $installScript) {
            # 同 install-windows.ps1 Invoke-Native 的理由：2>&1 的 stderr 是 ErrorRecord，
            # 直接 Write-Host 会打出「所在位置/CategoryInfo/NativeCommandError」红块。
            & $installScript 2>&1 | ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) { Write-Host ("    " + $_.Exception.Message) }
                else { Write-Host "    $_" }
            }
        }
    }
    $svc = Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
    if (-not $svc) { throw 'sshd 服务没注册（可手动跑 OpenSSH 目录下的 install-sshd.ps1）。' }

    Set-Service -Name 'sshd' -StartupType Automatic
    if ((Get-Service -Name 'sshd').Status -ne 'Running') { Start-Service -Name 'sshd' }
    Start-Sleep -Seconds 1
    $svc = Get-Service -Name 'sshd'
    Write-Host "  服务：$($svc.Status) / $($svc.StartType)" -ForegroundColor Green
    if ($svc.Status -ne 'Running') { throw 'sshd 没起来，先跑 sshd -T 看配置错误。' }

    Write-Step '防火墙规则'
    $name = 'OpenSSH-Server-Tailscale-In-TCP'
    $ranges = @('100.64.0.0/10', 'fd7a:115c:a1e0::/48')
    if ($AllowLan) { $ranges += 'LocalSubnet' }
    $existing = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if ($existing) {
        $addrList = @(($existing | Get-NetFirewallAddressFilter).RemoteAddress)
        if ($AllowLan -and ($addrList -notcontains 'LocalSubnet')) {
            $existing | Set-NetFirewallAddressFilter -RemoteAddress $ranges | Out-Null
            Write-Host "  已放宽为：$($ranges -join ', ')" -ForegroundColor Green
        }
        else {
            Write-Host "  已存在：$($addrList -join ', ')"
        }
    }
    else {
        $displayName = if ($AllowLan) { 'OpenSSH Server (Tailscale + LAN)' } else { 'OpenSSH Server (Tailscale only)' }
        New-NetFirewallRule -Name $name -DisplayName $displayName `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
            -RemoteAddress $ranges -Profile Any | Out-Null
        Write-Host "  已创建：$($ranges -join ', ')" -ForegroundColor Green
    }

    # Windows 装 OpenSSH Server 时可能已经自带「允许所有来源」的 22 端口规则；只建受限规则而不关它，
    # 「22 只对 tailnet 开放」就是假话。关之前先确认上面的受限规则确实在，否则会把自己锁在门外。
    #
    # 判据必须是**端口 22 + 来源 Any**，不能按名字猜：系统自带规则的 DisplayName 像
    # `OpenSSH Server (sshd)` / `OpenSSH SSH Server Preview (sshd)`，而它们的 Name 是
    # `OpenSSH-Server-In-TCP` 或者一个 GUID —— 把 DisplayName 当 -Name 传给
    # Get-NetFirewallRule 永远查不到，于是"已停用"是假话却看不出来（2026-11-03 真机发现）。
    $restricted = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if (-not $restricted) { throw "受限规则 $name 没建出来：先别继续，22 可能仍是全来源开放。" }

    # 逐条规则问它自己的端口过滤。**不能**把一批规则的过滤器收集成数组再按下标配回规则：有些规则
    # 没有端口过滤项，数组就短一截，下标整体错位 —— 2026-11-03 真机实测因此误停了两条 HNS 的
    # 53 端口规则（本该停 22 的，停到别人身上去了）。可复跑的判据见本文件 Tests 一节。
    $broadRules = @()
    foreach ($rule in @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue)) {
        if ($rule.Name -eq $name) { continue }
        if (-not (Test-FirewallRuleCoversPort -Rule $rule -Port $Port)) { continue }
        $addrs = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
        if ($addrs -contains 'Any') { $broadRules += $rule }
    }

    if ($broadRules.Count -eq 0) {
        Write-Host '  没有其它「所有来源」的 22 端口入站规则（无需收敛）' -ForegroundColor Green
    }
    foreach ($rule in $broadRules) {
        try {
            Disable-NetFirewallRule -Name $rule.Name -ErrorAction Stop
            Write-Host ("  已停用全来源规则：{0}（{1}）" -f $rule.Name, $rule.DisplayName) -ForegroundColor Yellow
        }
        catch {
            # 停不掉不是"警告"：那意味着 22 仍对任意来源开放，必须让安装失败而不是留个假的安全状态。
            throw ("停用全来源规则 {0}（{1}）失败：{2}。先手工禁用再重跑。" -f $rule.Name, $rule.DisplayName, $_.Exception.Message)
        }
    }

    return $sshdExe
}

function Test-Port([int]$Port) {
    # 光「连得上」不算数：WSL 的 localhost 转发（wslrelay）也占 127.0.0.1:22，
    # sshd 没起来时照样能 connect。必须读到真正的 SSH banner（^SSH-）。
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $client = New-Object Net.Sockets.TcpClient
        try {
            $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(3000)) {
                $client.EndConnect($iar)
                $stream = $client.GetStream()
                $stream.ReadTimeout = 3000
                $buffer = New-Object byte[] 64
                $read = $stream.Read($buffer, 0, $buffer.Length)
                if ($read -gt 0) {
                    $banner = [Text.Encoding]::ASCII.GetString($buffer, 0, $read)
                    if ($banner -match '^SSH-') { return $true }
                }
            }
        }
        catch { }
        finally { try { $client.Close() } catch { } }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Invoke-Main {
    $logPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'openssh-install-log.txt'
    try { Start-Transcript -Path $logPath -Force | Out-Null } catch { }

    Write-Host ''
    Write-Host '==========================================' -ForegroundColor Cyan
    Write-Host ' Windows OpenSSH Server 安装（远程调试用）' -ForegroundColor Cyan
    Write-Host '==========================================' -ForegroundColor Cyan
    Write-Host "计算机名: $env:COMPUTERNAME / 用户: $env:USERNAME"
    Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

    if (-not (Test-Administrator)) {
        Write-Host ''
        Write-Host '需要管理员权限，正在重新打开带 UAC 的窗口...' -ForegroundColor Yellow
        try { Stop-Transcript | Out-Null } catch { }
        $extra = @('-Port', "$Port")
        if ($AllowLan) { $extra += '-AllowLan' }
        if ($MsiPath) { $extra += @('-MsiPath', ('"{0}"' -f $MsiPath)) }
        $code = Invoke-ElevatedSelf -ExtraArgs $extra
        Write-Host ("提权窗口已结束，退出码 {0}。" -f $code) -ForegroundColor Yellow
        exit ([int]$code)
    }

    $ok = $false
    try {
        Install-SshdServer | Out-Null
        Write-Step '自检'
        $open = Test-Port -Port $Port
        Write-Host "  listen(127.0.0.1:$Port) = $open" -ForegroundColor Green
        if (Test-Path 'C:\ProgramData\ssh\administrators_authorized_keys') {
            Write-Host '  管理员公钥文件：存在' -ForegroundColor Green
        }
        Write-Host ''
        Write-Host '  好了，从 Mac 上连：' -ForegroundColor Cyan
        Write-Host ("    ssh {0}@{1}" -f $env:USERNAME, $env:COMPUTERNAME) -ForegroundColor Green
        $ok = $open
    }
    catch {
        Write-Host ''
        Write-Host "失败：$($_.Exception.Message)" -ForegroundColor Red
    }

    Write-Host ''
    Write-Host "日志：$logPath" -ForegroundColor Cyan
    try { Stop-Transcript | Out-Null } catch { }
    if (-not $NoPause) { Read-Host '按回车关闭' | Out-Null }
    if ($ok) { exit 0 } else { exit 1 }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Main }
