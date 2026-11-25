#Requires -Version 5.1
<#
.SYNOPSIS
  setup.exe 的安装编排：把「一台 Windows 从零到能 SSH 进去」拆成有顺序、可续跑的四步。

.DESCRIPTION
  这个脚本不含任何安装动作的**实现**：每一步都是调用仓库里已有的唯一实现，按顺序串起来，
  失败时把它自己的退出码原样传出去（谁的问题一眼能看出来）。

    P 前置  dist/wsl2/install-windows.ps1 -Mode Prereq   （WSL2/发行版/Linux 用户/依赖/体检）
    S 接入  tools/windows/setup-remote-debug.ps1          （Tailscale 入网 + OpenSSH + 公钥 + 端口）
    C 证书  tools/windows/install-gateway-cert.ps1       （网关根证书装到 Windows/WSL/Node）
    D DSH   dist/wsl2/wsl/install-dsh.sh                 （DSH + 三项配置 + key 文件）

  为什么是这个顺序：P 装出 WSL 和 Node（C/D 都要用）；S 让管理员能 SSH 进来（后面装 bridge 靠它）；
  C 让 WSL 里的 curl/node 认得内网网关（D 之后可能就要调模型）；D 最后，因为它要网络下载 npm 包。

  续跑：state.json 里每步写成 done 就不再重复；需要重启（3010）时由 Inno 的 RunOnce 接着跑。

.PARAMETER ConfigPath
  setup.exe 收集到的配置 JSON（见 Installer-Config.ps1 的说明）。

.PARAMETER AppDir
  setup.exe 的安装目录（{app}）。缺省 = 本脚本所在目录。

.PARAMETER LogPath
  日志。缺省 %ProgramData%\feishu-kit\install.log。

.PARAMETER PlanOnly
  只打印将要执行的命令和顺序，不执行任何东西（测试与人工核对用）。

.PARAMETER ResumeState
  从 state.json 续跑（RunOnce 用）：已 done 的步跳过。

.NOTES
  退出码：0 成功；2 前置环境；3 systemd；6 依赖；7 体检；8 接入层（Tailscale/sshd/公钥/端口）；
          9 证书；10 DSH；3010 需要重启 Windows。
#>
[CmdletBinding()]
param(
    # 不用 Mandatory：Mandatory 参数会在 dot-source（测试）时直接报错，连函数都加载不进来。
    # 缺参数由 Invoke-Main 开头那句检查负责，报错信息一样清楚。
    [string]$ConfigPath = '',
    [string]$AppDir = $PSScriptRoot,
    [string]$OfflineDir = '',
    [string]$LogPath = '',
    [switch]$PlanOnly,
    [switch]$ResumeState
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$script:AppDir = [IO.Path]::GetFullPath($AppDir)
# 载荷根：装好的机器上是 {app}\offline；在仓库里直接跑（开发/自测）时用 -OfflineDir 指到
# 构建目录（它的形状也是 repo\ + payload\，和安装后完全一样）。
if (-not $OfflineDir) { $OfflineDir = Join-Path $script:AppDir 'offline' }
$script:OfflineDir = [IO.Path]::GetFullPath($OfflineDir)
if (-not $LogPath) { $LogPath = Join-Path $env:ProgramData 'feishu-kit\install.log' }
$script:LogPath = $LogPath
$script:StateDir = Join-Path $env:ProgramData 'feishu-kit'
$script:StatePath = Join-Path $script:StateDir 'state.json'
$script:StageCodes = [ordered]@{ P = 2; S = 8; C = 9; D = 10 }
$script:StageNames = [ordered]@{
    P = '前置环境（WSL2 / 发行版 / Linux 用户 / 依赖 / 体检）'
    S = '远程接入（Tailscale 入网 / OpenSSH / 公钥 / 端口）'
    C = '网关证书（Windows / WSL / Node 三处）'
    D = 'DSH（程序 + 三项配置 + key 文件）'
}

# ------------------------------------------------------------------ 输出与日志

function Write-Log {
    param([string]$Text, [string]$Color = '')
    $line = '[{0}] {1}' -f (Get-Date).ToString('HH:mm:ss'), $Text
    try {
        New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    } catch { }
    if ($Color) { Write-Host $line -ForegroundColor $Color } else { Write-Host $line }
}

function Write-Step([string]$Text) {
    Write-Log ''
    Write-Log ('=== ' + $Text + ' ===') 'Cyan'
}

function Write-Progress2([int]$Percent, [string]$Text) {
    # 给安装界面/人看的进度的唯一格式：PROGRESS <百分比> <说明>
    Write-Log ("PROGRESS {0} {1}" -f $Percent, $Text)
}

function Write-StageFail([string]$Stage, [string]$Text) {
    Write-Log ("  [fail] " + $Text) 'Red'
    Write-Log ("STAGE_FAILED {0} {1}" -f $Stage, $Text)
    Save-LastFailure -Stage $Stage -Text $Text
}

function Save-LastFailure([string]$Stage, [string]$Text) {
    <#
      完成页只能显示一段短文字，同事也不会去翻几百行日志。所以把
      「哪一步、退出码多少、最后十几行输出」压成一个几百字的文件给完成页读。
      完整日志仍然是 install.log 那一份，这里是索引不是副本。
    #>
    try {
        $all = @()
        if (Test-Path -LiteralPath $script:LogPath) {
            $all = @(Get-Content -LiteralPath $script:LogPath -Encoding UTF8 | Where-Object { $_.Trim() -ne '' })
        }
        # 先挑「像原因」的行：失败时人要看的是 error/invalid/拒绝/缺失 那几句，
        # 而不是 JSON 报告里的缩进括号。挑不出来就退回最后 14 行。
        $needles = 'error|fail|invalid|problems|denied|refused|拒绝|失败|缺失|无法|未'
        $key = @($all | Where-Object { $_ -match $needles } | Select-Object -Last 6)
        $lines = @($all | Select-Object -Last 14)
        $target = Join-Path $script:StateDir 'last-failure.txt'
        $body = @()
        $body += ('阶段：' + $Stage + ' ' + $script:StageNames[$Stage])
        $body += ('结果：' + $Text)
        if ($key.Count -gt 0) {
            $body += ''
            $body += '关键行：'
            $body += $key
        }
        $body += ''
        $body += '最后 14 行日志（完整日志见 install.log）：'
        $body += $lines
        Set-Content -LiteralPath $target -Value $body -Encoding UTF8
    }
    catch { }
}

# ------------------------------------------------------------------ 状态文件

function Read-State {
    if (-not (Test-Path -LiteralPath $script:StatePath)) { return @{ stages = @{} } }
    try {
        $raw = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8
        $obj = $raw | ConvertFrom-Json
        $stages = @{}
        if ($obj.stages) {
            foreach ($property in $obj.stages.PSObject.Properties) { $stages[$property.Name] = [string]$property.Value }
        }
        return @{ stages = $stages }
    }
    catch {
        Write-Log ("  [warn] state.json 读不动（{0}），当成第一次安装" -f $_.Exception.Message) 'Yellow'
        return @{ stages = @{} }
    }
}

function Save-State {
    param([hashtable]$Stages, [string]$Extra = '')
    New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
    $ordered = [ordered]@{ updated_at = (Get-Date).ToString('s'); stages = [ordered]@{} }
    foreach ($key in $script:StageNames.Keys) {
        if ($Stages.ContainsKey($key)) { $ordered.stages[$key] = $Stages[$key] }
    }
    if ($Extra) { $ordered['note'] = $Extra }
    ($ordered | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
}

# ------------------------------------------------------------------ 配置

function Read-Config([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "配置文件不存在：$Path" }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    return ($raw | ConvertFrom-Json)
}

function Test-ConfigValue([object]$Config, [string]$Name) {
    $value = $Config.$Name
    if ($null -eq $value) { return $false }
    return ([string]$value).Trim().Length -gt 0
}

function Assert-Config([object]$Config) {
    # 只校验「缺了就一定装不成」的项。可选的（API key / 公钥）缺了要能装完并明确告知，不算错。
    $required = @(
        @{ Name = 'LinuxUser';  Message = 'Linux 用户名' },
        @{ Name = 'LinuxPassword'; Message = 'Linux 密码' },
        @{ Name = 'GatewayHost'; Message = 'AI 网关地址' },
        @{ Name = 'Provider'; Message = '渠道名' },
        @{ Name = 'ChannelVar'; Message = '渠道 key 的环境变量名' },
        @{ Name = 'DshVersion'; Message = 'DSH 版本' }
    )
    $missing = @()
    foreach ($item in $required) {
        if (-not (Test-ConfigValue $Config $item.Name)) { $missing += $item.Message }
    }
    if ($missing.Count -gt 0) { throw ("配置缺少：{0}" -f ($missing -join '、')) }
    if ($Config.LinuxPassword.Length -lt 8) { throw 'Linux 密码至少 8 位（WSL 的 passwd 会拒绝太短的密码）' }
    if (-not (Test-ConfigValue $Config 'Model')) { throw '配置缺少：默认模型' }
}

# ------------------------------------------------------------------ 调子脚本

function Invoke-ChildScript {
    <#
      在同一个 PowerShell 进程里跑子脚本（-LinuxPassword 是 SecureString，只能进程内传）。
      返回退出码：子脚本里的 exit N 只会结束它自己，不会带走这个进程。
    #>
    param([string]$ScriptPath, [hashtable]$Parameters = @{}, [switch]$Plan)
    if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "缺少子脚本：$ScriptPath" }
    if ($Plan) {
        $shown = @()
        foreach ($key in $Parameters.Keys) {
            $value = $Parameters[$key]
            if ($value -is [System.Security.SecureString]) { $shown += ('-' + $key + ' <SecureString>') }
            else { $shown += ('-' + $key + ' "' + [string]$value + '"') }
        }
        Write-Log ('  & "' + $ScriptPath + '" ' + ($shown -join ' '))
        return 0
    }
    $global:LASTEXITCODE = 0
    # 子脚本的输出要落进日志：只记「退出码 2」等于没有信息，原因都在它的输出里
    # （真机实例：Tailscale 报 "invalid key: unable to validate API key"，日志里却一个字都没有）。
    # 注意 6>&1：这些脚本的进度是用 Write-Host 打的，Write-Host 走的是 Information 流（6），
    # 只写 2>&1 抓不到，结果就是「控制台有、日志里没有」。
    $childOutput = & $ScriptPath @Parameters 6>&1 2>&1
    $code = [int]$LASTEXITCODE
    foreach ($line in @($childOutput)) {
        $text = [string]$line
        if ($text.Trim()) { Write-Log ('    ' + $text.TrimEnd()) }
    }
    return $code
}

# ------------------------------------------------------------------ WSL 内执行

function ConvertTo-WslPath([string]$WindowsPath) {
    $full = [IO.Path]::GetFullPath($WindowsPath)
    $drive = $full.Substring(0, 1).ToLower()
    $rest = ($full.Substring(2) -replace '\\', '/')
    return ('/mnt/' + $drive + $rest)
}

function Invoke-WslScript {
    <#
      wsl.exe 的命令行还会再过一层解析（$var / $(…) / ; 都被提前展开），所以脚本一律写文件、
      用 /mnt 路径执行。这个坑见 docs/HISTORY.md 踩坑 10。
    #>
    param([string]$Script, [string]$Distro, [switch]$Plan)
    $runDir = Join-Path $script:StateDir 'run'
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $local = Join-Path $runDir ('step-' + [guid]::NewGuid().ToString('N') + '.sh')
    [IO.File]::WriteAllText($local, ($Script -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
    try {
        $wslPath = ConvertTo-WslPath $local
        if ($Plan) {
            Write-Log ('  wsl -d ' + $Distro + ' -u root -- bash -lc ' + $wslPath)
            Write-Log ('  （脚本内容）' + (($Script -split "`n" | Select-Object -First 3) -join ' / ') + ' …')
            return 0
        }
        $output = & wsl.exe -d $Distro -u root -- bash -lc $wslPath 2>&1
        $code = $LASTEXITCODE
        foreach ($line in @($output)) { Write-Log ('    ' + [string]$line) }
        return [int]$code
    }
    finally { Remove-Item -LiteralPath $local -Force -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------ 各步

function Invoke-StagePrereq {
    param([object]$Config, [switch]$Plan)
    $installer = Join-Path $script:OfflineDir 'repo\dist\wsl2\install-windows.ps1'
    $payload = Join-Path $script:OfflineDir 'payload'
    $parameters = @{
        Mode = 'Prereq'
        Distro = $Config.Distro
        LinuxUser = $Config.LinuxUser
        LinuxPassword = (ConvertTo-SecureString ([string]$Config.LinuxPassword) -AsPlainText -Force)
        PayloadDir = $payload
        # 接入层（sshd/防火墙/公钥）统一由 setup-remote-debug.ps1 做，这里不重复调用 install-openssh.ps1。
        SkipOpenSsh = $true
        NoElevate = $true
    }
    return (Invoke-ChildScript -ScriptPath $installer -Parameters $parameters -Plan:$Plan)
}

function Invoke-StageAccess {
    param([object]$Config, [switch]$Plan)
    $scriptPath = Join-Path $script:OfflineDir 'repo\tools\windows\setup-remote-debug.ps1'
    $parameters = @{
        AuthKey = [string]$Config.TailscaleAuthKey
        HostName = [string]$Config.TailscaleHostName
        MacPubKey = [string]$Config.AdminPublicKey
        NoElevate = $true
        NoPause = $true
    }
    if (Test-ConfigValue $Config 'SshAllowLan') { $parameters['AllowLan'] = $true }
    return (Invoke-ChildScript -ScriptPath $scriptPath -Parameters $parameters -Plan:$Plan)
}

function Get-WindowsCaPemPath { return (Join-Path $script:StateDir 'certs\gateway-root.crt') }

function Invoke-StageCert {
    param([object]$Config, [switch]$Plan)
    # 证书安装的唯一实现（独立脚本，同事也可以单独跑它，不需要整套安装包）。
    $scriptPath = Join-Path $script:OfflineDir 'repo\tools\windows\install-gateway-cert.ps1'
    $parameters = @{
        GatewayHost = [string]$Config.GatewayHost
        Distro      = [string]$Config.Distro
        PemTarget   = (Get-WindowsCaPemPath)
        WithWsl     = $true
    }
    return (Invoke-ChildScript -ScriptPath $scriptPath -Parameters $parameters -Plan:$Plan)
}

function Invoke-StageDsh {
    param([object]$Config, [switch]$Plan)
    $scriptOnWindows = Join-Path $script:OfflineDir 'repo\dist\wsl2\wsl\install-dsh.sh'
    if (-not (Test-Path -LiteralPath $scriptOnWindows)) { throw "缺少 install-dsh.sh：$scriptOnWindows" }
    $runDir = Join-Path $script:StateDir 'run'
    $keyFile = Join-Path $runDir 'dsh-key.txt'
    $wrapped = '$0'  # 占位，下面替换
    $lines = @(
        'set -u',
        ('exec bash "{0}" --user "{1}" --provider "{2}" --channel-var "{3}" --base-url "{4}" --model "{5}" --dsh-version "{6}" --api-key-stdin < "{7}"' -f
            (ConvertTo-WslPath $scriptOnWindows), $Config.LinuxUser, $Config.Provider, $Config.ChannelVar,
            $Config.BaseUrl, $Config.Model, $Config.DshVersion, (ConvertTo-WslPath $keyFile))
    )
    $script = ($lines -join "`n")
    if (-not $Plan) {
        New-Item -ItemType Directory -Path $runDir -Force | Out-Null
        # key 只落这一个临时文件，装完立刻删；权限收紧到 Administrators/SYSTEM。
        [IO.File]::WriteAllText($keyFile, [string]$Config.ApiKey, (New-Object System.Text.UTF8Encoding($false)))
        try {
            & icacls.exe $keyFile /inheritance:r /grant:r '*S-1-5-32-544:F' '*S-1-5-18:F' 2>&1 | Out-Null
        }
        catch { }
    }
    try {
        $code = Invoke-WslScript -Script $script -Distro $Config.Distro -Plan:$Plan
    }
    finally { Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue }
    if ($code -eq 0 -and -not $Plan) {
        # WSL 侧装好了，但 Windows 原生的一方（官方 DSH 桌面端这类自带 runtime 的程序）
        # 读的是另一套东西，得单独补齐，否则「dsh web 能用、dsh desktop 报证书错/401」。
        $code = Set-WindowsDshAccess -Config $Config
    }
    return [int]$code
}

# ------------------------------------------------------------------ Windows 侧 DSH

# 这两个名字是**两套不同的 profile**，不是一个东西的两种叫法（别合并、别互相替代）：
#   desktop —— Electron 桌面端（DeepSeek Harness.exe）自己创建并独占管理的 profile。真机证据：
#              桌面端第一次启动会建 %USERPROFILE%\.dsh\profiles\desktop\，里面 cordis.patch.yml
#              开头就是「Your patch layer for this dsh profile」；
#              `dsh --profile desktop --dump-config` 被直接拒绝：
#              「profile "desktop" is managed exclusively by the Electron application」。
#   web     —— Windows 原生 `dsh web` CLI 的 profile（和 WSL 里那份同名，但内容互不相干）。
# 桌面端只读它自己那份：把渠道写进 web，桌面端不会多出任何 provider（真机 dump 对照：
# web 里有 acme-ai-channel，desktop 里没有）。所以 Windows 侧必须写 desktop；
# 机器上同时有 web（说明也用过 CLI）就两处都写，各自服务各自的入口。
$script:DshPatchMarkerBegin = '# >>> feishu-kit 安装程序写入（重跑会整段替换这一段）'
$script:DshPatchMarkerEnd = '# <<< feishu-kit'

function Get-DshProfilePatchYaml {
    <#
      profile patch 的 AI 配置：provider / apiKeyEnv / baseURL + **模型清单** + 默认模型。
      模型清单来自网关的 /v1/models（见 Get-GatewayModelIds）；不传就退回配置里那一个默认模型，
      所以「纯函数、可单测」和「清单跟着网关走」两件事不冲突。
    #>
    param([object]$Config, [string[]]$Models = @())
    if (-not $Models -or $Models.Count -eq 0) { $Models = @([string]$Config.Model) }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# 由安装程序生成：AI 渠道（provider / apiKeyEnv / baseURL）+ 模型清单 + 默认模型。')
    $lines.Add('# 模型清单取自网关的 /v1/models（网关加/删模型后重跑安装即可同步）。')
    $lines.Add('# 与 WSL 里 ~/.dsh/profiles/web/cordis.patch.yml 同形状，两边可以直接对照。')
    $lines.Add('- id: llm-pi-ai')
    $lines.Add('  config:')
    $lines.Add('    providers:')
    $lines.Add(('      ' + $Config.Provider + ':'))
    $lines.Add(('        apiKeyEnv: ' + $Config.ChannelVar))
    $lines.Add('        api: openai-completions')
    $lines.Add(('        baseURL: ' + $Config.BaseUrl))
    $lines.Add('        models:')
    foreach ($mid in $Models) {
        $lines.Add(('          - id: ' + $mid))
        $lines.Add(('            name: ' + $mid))
    }
    $lines.Add('- id: agent-default-model')
    $lines.Add('  config:')
    $lines.Add(('    provider: ' + $Config.Provider))
    $lines.Add(('    model: ' + $Config.Model))
    return ($lines -join "`r`n")
}

function Merge-ModelIds {
    <#
      去重，并保证**默认模型在清单里**：agent-default-model 指向一个不在清单里的 id，
      模型选择器就会显示一个永远选不中的模型。网关返回的顺序保持不变（默认模型只在缺失时补到最前面）。
    #>
    param([string[]]$Models = @(), [string]$Default = '')
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($m in $Models) {
        if (-not $m) { continue }
        if (-not $out.Contains($m)) { $out.Add($m) }
    }
    if ($Default -and -not $out.Contains($Default)) { $out.Insert(0, $Default) }
    return $out.ToArray()
}

function Get-GatewayModelIds {
    <#
      网关的模型清单 = 「这台机器上该有哪些模型」的**唯一来源**：GET <base>/v1/models（OpenAI 约定）。
      以前这里写的是配置里那一个默认模型，于是同事的桌面端/CLI 模型选择器里只有一个，
      而网关实际服务好几个（真机见 HISTORY 踩坑 22）。清单是网关的事实，抄一份到配置里迟早漂。

      取不到（网关不提供该端点 / key 无权限 / 网络）就退回那一个默认模型，**并且打 warn**：
      静默少给是最难发现的一类错。
    #>
    param([object]$Config)
    $base = ([string]$Config.BaseUrl).TrimEnd('/') -replace '/v1$', ''
    $url = $base + '/v1/models'
    try {
        $headers = @{}
        if ([string]$Config.ApiKey) { $headers['Authorization'] = 'Bearer ' + [string]$Config.ApiKey }
        $resp = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
        $ids = @($resp.data | ForEach-Object { [string]$_.id } | Where-Object { $_ })
        if ($ids.Count -gt 0) {
            $models = @(Merge-ModelIds -Models $ids -Default ([string]$Config.Model))
            Write-Log ('  [ok] 网关模型清单 ' + $models.Count + ' 个：' + ($models -join ', '))
            return $models
        }
        Write-Log ('  [warn] ' + $url + ' 没返回模型：只写默认模型 ' + [string]$Config.Model)
    }
    catch {
        Write-Log ('  [warn] 取不到 ' + $url + '（' + $_.Exception.Message + '）：只写默认模型 ' + [string]$Config.Model)
    }
    return @([string]$Config.Model)
}

function Get-DshProfilePatchBlock {
    <# 带标记的一段：并进别人的 cordis.patch.yml 时靠这两行定位，重跑才换得掉、且不动别人的条目。 #>
    param([object]$Config, [string[]]$Models = @())
    return (@(
        $script:DshPatchMarkerBegin
        (Get-DshProfilePatchYaml -Config $Config -Models $Models)
        $script:DshPatchMarkerEnd
    ) -join "`r`n")
}

function Select-DshPatchProfileNames {
    <# 该写哪些 profile：desktop 优先（桌面端），其次 web（Windows 原生 CLI）。不存在的不管。 #>
    param([string[]]$ExistingProfiles = @())
    return @(@('desktop', 'web') | Where-Object { $ExistingProfiles -contains $_ })
}

function Get-DshPatchRootKind {
    <#
      已有 cordis.patch.yml 的**根节点**是哪一种。DSH 的这个文件必须是「顶层数组」，
      往里放 `- id:` 条目只有根是块序列（或还没有根）时才成立：
        empty      —— 空文件 / 只有注释：条目追加进去正好成为根序列；
        empty-flow —— 正文就是一行 `[]`：桌面端第一次启动建出来、用户还没改过任何设置的样子；
        sequence   —— 根已经是块序列：追加到末尾；
        other      —— 根是映射或非空 flow 序列：再放条目就是「文档已结束，后面又来一个根节点」。
      只看非注释行就够判断（这个文件里没有别的根形状）。
    #>
    param([string]$Existing = '')
    $meaningful = New-Object System.Collections.Generic.List[string]
    if ($Existing) {
        foreach ($line in ($Existing -split "`r?`n")) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            $meaningful.Add($t)
        }
    }
    if ($meaningful.Count -eq 0) { return 'empty' }
    if ($meaningful.Count -eq 1 -and $meaningful[0] -eq '[]') { return 'empty-flow' }
    if ($meaningful[0].StartsWith('-')) { return 'sequence' }
    return 'other'
}

function Merge-DshProfilePatch {
    <#
      把渠道配置并进已有的 cordis.patch.yml：
        - 有我们写的标记段 → 整段替换（重跑幂等，不会越来越长）；
        - 没有 → 追加到末尾，原有条目一个字都不动。
      为什么不能整份覆盖：桌面端那份文件里有它自己的条目（ui-chat / ui-settings…），
      整份覆盖等于把用户的界面设置清掉；而且下次桌面端可能再写回来，两边互相抹。

      **根是 `[]` 的那一行必须先删掉**（真机见 HISTORY 踩坑 21）：桌面端第一次启动建出的 profile 里，
      这个文件的正文就是一个空数组 `[]` —— 它在那一行就把文档结束了，后面再追加或替换出 `- id:`，
      YAML 就报「end of the stream or a document separator is expected」，
      桌面端启动直接 DesktopHostFatalError，窗口根本不出现。删掉 `[]` 后我们的条目正好顶成根序列。

      写出去之前再判一次根形状：只要不是序列就抛错、一个字节都不落盘。
      宁可让 D 阶段报 fail 让人来看，也不能再写出一份连解析都过不了的文件。
    #>
    param([string]$Existing = '', [string]$Block = '')
    $begin = $script:DshPatchMarkerBegin
    $end = $script:DshPatchMarkerEnd
    $blockLines = @($Block -split "`r?`n")
    $lines = New-Object System.Collections.Generic.List[string]
    if ($Existing) { foreach ($line in ($Existing -split "`r?`n")) { $lines.Add($line) } }

    # 空数组根（桌面端还没写过任何设置时的样子）：留着它，新条目就成了第二个根节点。
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        if ($lines[$i].Trim() -eq '[]') { $lines.RemoveAt($i) }
    }

    $startIdx = -1
    $endIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq $begin) { $startIdx = $i; break }
    }
    if ($startIdx -ge 0) {
        for ($j = $startIdx + 1; $j -lt $lines.Count; $j++) {
            if ($lines[$j].Trim() -eq $end) { $endIdx = $j; break }
        }
        if ($endIdx -gt $startIdx) {
            $lines.RemoveRange($startIdx, ($endIdx - $startIdx + 1))
            $lines.InsertRange($startIdx, [string[]]$blockLines)
        }
    }
    if ($startIdx -lt 0 -or $endIdx -le $startIdx) {
        while ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -eq '') { $lines.RemoveAt($lines.Count - 1) }
        if ($lines.Count -gt 0) { $lines.Add('') }
        foreach ($line in $blockLines) { $lines.Add($line) }
    }

    $merged = (($lines -join "`r`n").TrimEnd())
    if ((Get-DshPatchRootKind -Existing $merged) -ne 'sequence') {
        throw '已有的 cordis.patch.yml 根节点不是 YAML 数组：追加条目会写出两个根节点，拒绝落盘'
    }
    return $merged
}

function Set-WindowsDshAccess {
    <#
      Windows 原生跑 DSH（官方桌面端自带 node/runtime）和 WSL 里的 dsh web 读的是两套东西：

        证书：Windows 程序都信 Windows 证书库（Chromium 窗口没问题），
              但 **node 不读 Windows 证书库**，只认 NODE_EXTRA_CA_CERTS 指向的 PEM
              —— 真机实测：Windows 侧 node 直连网关报 SELF_SIGNED_CERT_IN_CHAIN，加上就通。
        key ：WSL 那份在 /etc/feishu-bridge/dsh.env（0600 root），Windows 进程根本读不到。

      所以补三样：PEM 路径、用户级环境变量、**桌面端自己那个 profile** 的 patch
      （`%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml`，见上面 desktop/web 的说明）。
      写入范围是「安装时这个 Windows 账号」——同一账号下的程序都能读到 key，这是桌面端能用的
      前提；WSL 侧仍然保持 0600 root 的更严做法。
    #>
    param([object]$Config, [switch]$Plan)
    $pem = Get-WindowsCaPemPath
    $fail = 0
    $homeDir = $env:USERPROFILE
    $dshHome = Join-Path $homeDir '.dsh'
    $profilesRoot = Join-Path $dshHome 'profiles'

    if ($Plan) {
        Write-Log ('  会把 NODE_EXTRA_CA_CERTS 设成 ' + $pem + '（用户级）')
        Write-Log ('  会把 DEEPSEEK_API_KEY / ' + $Config.ChannelVar + ' 设成用户级环境变量')
        Write-Log ('  会往 profiles\desktop\cordis.patch.yml（桌面端）' + $(if (Test-Path (Join-Path $profilesRoot 'web')) { ' 和 profiles\web\cordis.patch.yml（原生 CLI）' } else { '' }) + ' 里并进渠道配置')
        Write-Log ('  模型清单会先问网关 ' + ([string]$Config.BaseUrl).TrimEnd('/') + '/models（取不到才只写默认模型）')
        return 0
    }

    Write-Log '  ==> Windows 侧（桌面端 / 原生 node）'

    # 1) 证书：PEM + NODE_EXTRA_CA_CERTS
    if (-not (Test-Path -LiteralPath $pem)) {
        Write-Log ('  [fail] 找不到 PEM：' + $pem + '（C 阶段没导出成功）')
        $fail = 1
    }
    else {
        [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $pem, 'User')
        Write-Log ('  [ok] NODE_EXTRA_CA_CERTS → ' + $pem)
        # 就地验证：新进程按注册表里的值读，连一次网关。401 = 证书通了只是没 key，
        # SELF_SIGNED_CERT_IN_CHAIN = 证书没生效。
        $node = (Get-Command node.exe -ErrorAction SilentlyContinue)
        if ($node) {
            $probe = Join-Path $script:StateDir 'run\probe-tls.js'
            [IO.File]::WriteAllText($probe, @'
const https = require("https");
const url = process.argv[2] + "/v1/models";
https.get(url, (r) => { console.log("WIN_NODE_HTTP=" + r.statusCode); process.exit(0); })
  .on("error", (e) => { console.log("WIN_NODE_ERR=" + (e.code || e.message)); process.exit(0); });
'@, (New-Object System.Text.UTF8Encoding($false)))
            $saved = $env:NODE_EXTRA_CA_CERTS
            try {
                $env:NODE_EXTRA_CA_CERTS = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
                $out = (& $node.Source $probe ([string]$Config.BaseUrl -replace '/v1$', '') 2>&1 | Out-String).Trim()
                Write-Log ('  ' + $out)
                if ($out -notmatch 'WIN_NODE_HTTP=(200|401|403|404)') {
                    Write-Log '  [fail] Windows 原生 node 仍不信任网关证书（NODE_EXTRA_CA_CERTS 没生效）'
                    $fail = 1
                }
            }
            catch { Write-Log ('  [warn] 就地验证没跑成：' + $_.Exception.Message) }
            finally { $env:NODE_EXTRA_CA_CERTS = $saved; Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue }
        }
        else { Write-Log '  [warn] 这台机器没有 node.exe，跳过就地验证（桌面端自带 runtime，不受影响）' }
    }

    # 2) key：用户级环境变量（桌面端从 Windows 进程环境读）
    if ([string]$Config.ApiKey) {
        [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', [string]$Config.ApiKey, 'User')
        [Environment]::SetEnvironmentVariable([string]$Config.ChannelVar, [string]$Config.ApiKey, 'User')
        Write-Log ('  [ok] DEEPSEEK_API_KEY / ' + $Config.ChannelVar + ' 已设为用户级环境变量（已打开的桌面端要重启才读得到）')
    }
    else {
        Write-Log '  [warn] cfg 里没有 API key：桌面端/原生 node 调用会 401，需要自己补 key'
    }

    # 3) profile 配置：并进桌面端自己的 profile（profiles\desktop），有原生 CLI 的话连 web 一起
    #    模型清单先问网关（/v1/models）：机器上有哪些模型是网关的事实，不在这里抄一份。
    $models = @(Get-GatewayModelIds -Config $Config)
    $block = Get-DshProfilePatchBlock -Config $Config -Models $models
    $existingProfiles = @()
    if (Test-Path -LiteralPath $profilesRoot) {
        $existingProfiles = @(Get-ChildItem -LiteralPath $profilesRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Name })
    }
    $patchProfiles = @(Select-DshPatchProfileNames -ExistingProfiles $existingProfiles)
    if ($patchProfiles.Count -gt 0) {
        foreach ($name in $patchProfiles) {
            $target = Join-Path (Join-Path $profilesRoot $name) 'cordis.patch.yml'
            $old = ''
            if (Test-Path -LiteralPath $target) { $old = Get-Content -LiteralPath $target -Raw -Encoding UTF8 }
            try { $merged = Merge-DshProfilePatch -Existing $old -Block $block }
            catch {
                Write-Log ('  [fail] 不敢写 ' + $target + '：' + $_.Exception.Message)
                Write-Log '         这份文件必须是顶层 YAML 数组（条目以 - id: 开头）。把它改成 [] 或删掉，然后重跑安装'
                $fail = 1
                continue
            }
            Set-Content -LiteralPath $target -Value $merged -Encoding UTF8
            Write-Log ('  [ok] 已写入 ' + $target + '（' + $name + ' profile）')
        }
        if ($patchProfiles -notcontains 'desktop') {
            Write-Log '  [warn] 没找到 profiles\desktop：桌面端还没打开过一次，配置对桌面端暂不生效；打开一次再重跑安装即可补上'
        }
    }
    else {
        # profile 目录还没生成（桌面端没跑过）：不猜它的目录结构，放一份待用文件。
        $pending = Join-Path $dshHome 'feishu-kit-cordis.patch.yml'
        New-Item -ItemType Directory -Path $dshHome -Force | Out-Null
        Set-Content -LiteralPath $pending -Value $block -Encoding UTF8
        Write-Log ('  [warn] 还没 ' + (Join-Path $profilesRoot 'desktop') + '（桌面端没跑过）：配置放在 ' + $pending)
        Write-Log '         打开一次 DSH Desktop 后，把这份文件里的内容并进 profiles\desktop\cordis.patch.yml 即可'
    }

    if ($fail -ne 0) { return 7 }
    return 0
}

# ------------------------------------------------------------------ 报告

function Get-SshTarget {
    <#
      ssh 登录的是 **Windows 账号**：Windows 的 sshd 拿用户名去校验本机账号，它不知道 WSL 里
      有谁。`Config.LinuxUser` 是 WSL 里的账号，只是**默认**跟着 Windows 用户名走，所以两者
      在多数机器上恰好同名，这个区别看不出来；不同名时（例如 Windows 名是中文、推导成 dsh）
      按 LinuxUser 写就会连到不存在的账号。
    #>
    param([object]$Config)
    $hint = [string]$Config.TailscaleHostName
    if (-not $hint) { $hint = $env:COMPUTERNAME.ToLower() }
    $login = $env:USERNAME
    if (-not $login) { $login = [string]$Config.LinuxUser }
    return ('ssh {0}@{1}' -f $login, $hint)
}

function Write-Report {
    param([object]$Config, [hashtable]$Stages)
    $lines = @()
    $lines += '# feishu-kit 安装报告'
    $lines += ''
    $lines += ('- 时间：{0}' -f (Get-Date).ToString('s'))
    $lines += ('- 计算机：{0}' -f $env:COMPUTERNAME)
    $lines += ('- 日志：{0}' -f $script:LogPath)
    $lines += ''
    $lines += '| 步骤 | 结果 |'
    $lines += '| --- | --- |'
    foreach ($key in $script:StageNames.Keys) {
        $state = '未执行'
        if ($Stages.ContainsKey($key)) { $state = $Stages[$key] }
        $lines += ('| {0} | {1} |' -f $script:StageNames[$key], $state)
    }
    $lines += ''
    $lines += '## 下一步（在 Mac 上执行）'
    $lines += ''
    $lines += '```'
    $lines += (Get-SshTarget -Config $Config)
    $lines += '```'
    $lines += ''
    $lines += '进去以后装 bridge：`cd ~/feishu-personal-agent && bash dist/wsl2/install-dsh-consumer.sh`'
    $lines += ''
    $lines += '## Windows 桌面端（官方 DSH 桌面端）'
    $lines += ''
    $lines += ('- 证书 PEM：`{0}`（已设用户级 `NODE_EXTRA_CA_CERTS`，node 不读 Windows 证书库）' -f (Get-WindowsCaPemPath))
    $lines += ('- API key：用户级环境变量 `DEEPSEEK_API_KEY` / `{0}`' -f $Config.ChannelVar)
    $lines += ('- profile 配置：`{0}`（桌面端只认自己这份 profile）' -f (Join-Path $env:USERPROFILE '.dsh\profiles\desktop\cordis.patch.yml'))
    $lines += '- 装完先**完全退出桌面端再打开**（环境变量是进程启动时读的，已打开的那个还没生效）'
    $reportPath = Join-Path ([Environment]::GetFolderPath('Desktop')) ('feishu-kit-install-{0}.md' -f (Get-Date).ToString('yyyyMMdd-HHmmss'))
    if (-not $PlanOnly) {
        try { ($lines -join "`n") | Set-Content -LiteralPath $reportPath -Encoding UTF8 }
        catch { Write-Log ("  [warn] 报告写不到桌面：{0}" -f $_.Exception.Message) 'Yellow' }
    }
    Write-Log ('  报告：' + $reportPath)
    return $reportPath
}

# ------------------------------------------------------------------ 主流程

function Test-SamePath([string]$Left, [string]$Right) {
    # 续跑时源就是目标，得先比出「同一个文件」再看要不要复制（Copy-Item 对同一文件会抛错）。
    try {
        $l = [IO.Path]::GetFullPath($Left)
        $r = [IO.Path]::GetFullPath($Right)
        return [string]::Equals($l, $r, [StringComparison]::OrdinalIgnoreCase)
    }
    catch { return $false }
}

function Save-ConfigSnapshot([object]$Config, [string]$SourcePath) {
    # setup.exe 的 {tmp} 目录在安装结束后会被删掉，而 RunOnce 续跑还要读配置，
    # 所以留一份快照。里面有 key：ACL 收紧到 Administrators/SYSTEM，成功装完后删除。
    #
    # 续跑（RunOnce 传 -ConfigPath 指向这份快照）时源和目标**是同一个文件**，这时不能再复制：
    # Copy-Item 会抛「无法使用项 … 其自身覆盖该项」，而且在 Invoke-Main 的最前面就抛，
    # 整套安装当场中止、RunOnce 又被 Windows 用掉了 —— 真机踩过（需要重启的机器重启后一进来就死）。
    $target = Join-Path $script:StateDir 'install-config.json'
    New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
    if (-not (Test-SamePath $SourcePath $target)) {
        Copy-Item -LiteralPath $SourcePath -Destination $target -Force
    }
    try { & icacls.exe $target /inheritance:r /grant:r '*S-1-5-32-544:F' '*S-1-5-18:F' 2>&1 | Out-Null } catch { }
    return $target
}

function Register-ResumeRunOnce([string]$ConfigSnapshot) {
    # 重启后自动接着装：RunOnce 只跑一次；-NoExit 让窗口留着，失败时看得见报错。
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'
    $command = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -File "{0}" -ConfigPath "{1}" -AppDir "{2}" -ResumeState' -f
        (Join-Path $script:AppDir 'apply.ps1'), $ConfigSnapshot, $script:AppDir)
    try {
        New-Item -Path $key -Force | Out-Null
        Set-ItemProperty -Path $key -Name 'FeishuKitResume' -Value ('cmd /c ' + $command)
        Write-Log '  已登记 RunOnce：FeishuKitResume'
    }
    catch { Write-Log ("  [warn] 登记 RunOnce 失败：{0}（重启后请手动重跑 setup.exe）" -f $_.Exception.Message) 'Yellow' }
}

function Invoke-Main {
    if (-not $ConfigPath) { throw '缺少 -ConfigPath（setup.exe 会传；手工跑见 README）' }
    Write-Log ('feishu-kit 安装器开始：{0}' -f (Get-Date).ToString('s'))
    if ($PlanOnly) { Write-Log '（PlanOnly：只打印计划，不执行）' 'Yellow' }

    Remove-Item -LiteralPath (Join-Path $script:StateDir 'install-ok.flag') -Force -ErrorAction SilentlyContinue
    $config = Read-Config $ConfigPath
    Assert-Config $config
    $configSnapshot = ''
    if (-not $PlanOnly) { $configSnapshot = Save-ConfigSnapshot -Config $config -SourcePath $ConfigPath }
    Write-Log ('载荷根：' + $script:OfflineDir)
    Write-Log ('配置：Linux 用户 {0} / 发行版 {1} / 渠道 {2} / 模型 {3} / 网关 {4}' -f
        $config.LinuxUser, $config.Distro, $config.Provider, $config.Model, $config.GatewayHost)

    $state = Read-State
    $stages = $state.stages
    if (-not $ResumeState) { $stages = @{} }
    if ($stages.Count -gt 0) {
        $pairs = @()
        foreach ($name in $stages.Keys) { $pairs += ('{0}={1}' -f $name, $stages[$name]) }
        Write-Log ('续跑状态：' + ($pairs -join '，'))
    }
    else { Write-Log '续跑状态：（无，从头装）' }
    Save-State -Stages $stages

    $plan = [ordered]@{
        P = @{ Name = $script:StageNames.P; Percent = 10; Run = { Invoke-StagePrereq -Config $config -Plan:$PlanOnly } }
        S = @{ Name = $script:StageNames.S; Percent = 45; Run = { Invoke-StageAccess -Config $config -Plan:$PlanOnly } }
        C = @{ Name = $script:StageNames.C; Percent = 70; Run = { Invoke-StageCert -Config $config -Plan:$PlanOnly } }
        D = @{ Name = $script:StageNames.D; Percent = 85; Run = { Invoke-StageDsh -Config $config -Plan:$PlanOnly } }
    }

    foreach ($key in $plan.Keys) {
        if ($stages.ContainsKey($key) -and $stages[$key] -eq 'done') {
            Write-Log ("跳过 {0}（上次已完成）" -f $plan[$key].Name) 'DarkGray'
            continue
        }
        Write-Step ("{0} {1}" -f $key, $plan[$key].Name)
        Write-Progress2 $plan[$key].Percent $plan[$key].Name
        $code = & $plan[$key].Run
        if ($code -eq 0) {
            $stages[$key] = 'done'
            Save-State -Stages $stages
            Write-Log '  [ok] 完成' 'Green'
            continue
        }
        if ($code -eq 3010) {
            $stages[$key] = 'reboot_pending'
            Save-State -Stages $stages -Extra '需要重启 Windows 后继续'
            if ($configSnapshot) { Register-ResumeRunOnce -ConfigSnapshot $configSnapshot }
            Write-Log '  [reboot] 需要重启 Windows 才能继续（已登记 RunOnce，重启后自动接着装）' 'Yellow'
            Write-Log 'INSTALL_NEEDS_REBOOT'
            return 3010
        }
        Write-StageFail $key ("退出码 {0}" -f $code)
        return [int]$code
    }

    Write-Progress2 95 '写报告'
    $report = Write-Report -Config $config -Stages $stages
    Save-State -Stages $stages -Extra '完成'
    # 给安装界面的完成页看的两样东西：结果标记 + 该敲的 ssh 命令
    try {
        Set-Content -LiteralPath (Join-Path $script:StateDir 'ssh-target.txt') -Value (Get-SshTarget -Config $config) -Encoding UTF8
        Set-Content -LiteralPath (Join-Path $script:StateDir 'install-ok.flag') -Value ((Get-Date).ToString('s')) -Encoding UTF8
    }
    catch { }
    # 装完了就把带 key 的配置快照删掉（日志里没有 key）
    if ($configSnapshot) { Remove-Item -LiteralPath $configSnapshot -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath (Join-Path $script:StateDir 'last-failure.txt') -Force -ErrorAction SilentlyContinue
    try { Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'FeishuKitResume' -ErrorAction SilentlyContinue } catch { }
    Write-Log ''
    Write-Log 'INSTALL_OK' 'Green'
    Write-Log ('  在 Mac 上执行：' + (Get-SshTarget -Config $config)) 'Green'
    Write-Log ('  报告：' + $report)
    return 0
}

# 只有真跑（不是被 dot-source）时才执行：测试要能 dot-source 进来单测纯函数。
if ($MyInvocation.InvocationName -ne '.') {
    try {
        exit (Invoke-Main)
    }
    catch {
        Write-Log ("安装中止：{0}" -f $_.Exception.Message) 'Red'
        Write-Log ("  位置：{0}" -f $_.InvocationInfo.PositionMessage)
        Write-Log 'INSTALL_FAILED 1'
        exit 1
    }
}
