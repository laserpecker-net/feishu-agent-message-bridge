#Requires -Version 5.1
<#
.SYNOPSIS
  只装网关根证书：让这台 Windows 机器信任内网 newapi 网关（DSH 桌面端 / 原生 node / 浏览器）。

.DESCRIPTION
  一个文件，只干一件事：把网关的根证书装进「真正会用到它的地方」，然后当场验证。

  Windows 上「信任一张自签 CA」需要两处，缺一处就会出现「有的能通、有的报
  SELF_SIGNED_CERT_IN_CHAIN」这种最难查的状态：

    1. Windows 证书库（管理员时装 LocalMachine\Root，这是权威锚点；非管理员时装 CurrentUser\Root）
       —— Chromium/Electron 窗口、Invoke-WebRequest、curl.exe、系统组件走这里。
    2. 环境变量 NODE_EXTRA_CA_CERTS 指向的 PEM 文件
       —— **Node 完全不读 Windows 证书库**，只认自己的 bundle。
       DSH 桌面端（DeepSeek Desktop，Electron）就是「窗口走证书库 + Node 走环境变量」的组合：
       少装任一处，桌面端会表现为「界面能开、一问模型就报证书错误」。
       注意：环境变量只影响**新启动**的进程，装完必须完全退出桌面端再打开（必要时注销重登）。

  证书是现抓的，脚本里不内置任何指纹、不内置任何内网地址：网关换证后重跑即可。
  只装链里的根（CA:TRUE 的自签证书），中间证书由服务端下发，不入库。

  装完会做三条真实验证（不是"看起来装上了"）：
    - .NET/Windows 证书库：TLS 握手拿状态码（401/200 = 证书通了，只是没 key）
    - curl.exe（schannel，走用户证书库）
    - node（走 NODE_EXTRA_CA_CERTS）

.PARAMETER GatewayHost
  网关主机（IP 或域名）。脚本不内置默认值：内网地址不写死进代码。

.PARAMETER Port
  默认 443。

.PARAMETER PemPath
  不给 -GatewayHost 时用本地根证书文件（PEM 或 DER 的 .cer/.crt）。离线或网关暂不可达时用。

.PARAMETER ProbePath
  验证用的请求路径，默认 /v1/models。返回 401/403/404 都算「TLS 通了」。

.PARAMETER Uninstall
  反向操作：把这张根证书从两个证书库删掉，并清掉 NODE_EXTRA_CA_CERTS。

.PARAMETER DryRun
  只抓链 + 打印指纹和当前是否已信任，不做任何修改。

.PARAMETER WithWsl
  额外把根证书装进 WSL 发行版的 ca-certificates（默认不装：只用 Windows 桌面端时不需要）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-gateway-cert.ps1 -GatewayHost <网关IP或域名> -DryRun

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-gateway-cert.ps1 -GatewayHost <网关IP或域名>
#>
param(
    [string]$GatewayHost = '',
    [int]$Port = 443,
    [string]$PemPath = '',
    [string]$ProbePath = '/v1/models',
    [string]$PemTarget = '',
    [string]$Distro = 'Ubuntu',
    [switch]$WithWsl,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Step([string]$Text) { Write-Host ''; Write-Host "=== $Text ===" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "  [warn] $Text" -ForegroundColor Yellow }
function Write-Err([string]$Text) { Write-Host "  [fail] $Text" -ForegroundColor Red }

function Get-TlsChain {
    # 抓服务端证书链。PS 5.1 上这里有两个坑，都是真机实测出来的：
    #   1) 不能用 SslStream：它的 RemoteCertificateValidationCallback 拿到的 X509Chain 里
    #      ChainElements 恒为空（握手期间还没填）。
    #   2) 用 HttpWebRequest + ServicePointManager.ServerCertificateValidationCallback 能拿到填好的链，
    #      但**必须在回调内部就把证书取出来**：同一个 X509Chain 实例在握手结束后会被清空。
    # 回调返回 $true：我们只取链，信不信由后面「装两处 + 逐处验证」判定，不靠这一次握手。
    param([string]$TargetHost, [int]$TargetPort, [int]$TimeoutMs = 10000)
    $url = ('https://{0}:{1}/?cert-probe={2}' -f $TargetHost, $TargetPort, [guid]::NewGuid().ToString('N'))
    $script:certProbeChain = $null
    $previous = [Net.ServicePointManager]::ServerCertificateValidationCallback
    try {
        [Net.ServicePointManager]::ServerCertificateValidationCallback = {
            param($sender, $certificate, $chain, $errors)
            $collected = @()
            if ($chain) {
                foreach ($element in @($chain.ChainElements)) { $collected += $element.Certificate }
            }
            if ($collected.Count -eq 0 -and $certificate) {
                $collected += (New-Object Security.Cryptography.X509Certificates.X509Certificate2($certificate))
            }
            $script:certProbeChain = $collected
            return $true
        }
        $request = [Net.HttpWebRequest]::Create($url)
        $request.Timeout = $TimeoutMs
        $request.ReadWriteTimeout = $TimeoutMs
        $request.KeepAlive = $false
        $request.UserAgent = 'install-gateway-cert'
        try {
            $response = $request.GetResponse()
            $response.Close()
        }
        catch {
            # 非 2xx（例如 404）说明 TLS 已经握手成功、回调已经跑过；只有连不上/超时才当失败。
            if (-not $script:certProbeChain) { throw "连不上 ${url}：$($_.Exception.Message)" }
        }
        if (-not $script:certProbeChain) { throw "没抓到证书链（${url} 没有完成 TLS 握手）" }
        # 显式装进对象里返回：PowerShell 的「返回数组」会把单元素链拆成标量、把嵌套数组原样带出，
        # 调用方一不留神就拿到 object[]（真机故障：Remove/Add 报参数转换错误）。
        return [pscustomobject]@{ Certs = [object[]]$script:certProbeChain }
    }
    finally {
        [Net.ServicePointManager]::ServerCertificateValidationCallback = $previous
    }
}

function Test-IsCa([Security.Cryptography.X509Certificates.X509Certificate2]$Cert) {
    # 只看 BasicConstraints.CertificateAuthority，不猜名字。
    foreach ($ext in $Cert.Extensions) {
        if ($ext -is [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]) {
            return [bool]$ext.CertificateAuthority
        }
    }
    return $false
}

function ConvertTo-Pem([Security.Cryptography.X509Certificates.X509Certificate2]$Cert) {
    # 形状必须对：每行 64 宽 + 结尾换行，否则 ca-certificates 会报 no newline。
    $b64 = [Convert]::ToBase64String($Cert.RawData)
    $lines = @()
    for ($i = 0; $i -lt $b64.Length; $i += 64) {
        $lines += $b64.Substring($i, [Math]::Min(64, $b64.Length - $i))
    }
    return ((@('-----BEGIN CERTIFICATE-----') + $lines + @('-----END CERTIFICATE-----')) -join "`n") + "`n"
}

function Test-StoreTrusted {
    # 同一个判定函数查两个库：两个库的结论必须分开说，不能合并成一句"已信任"。
    param([Security.Cryptography.X509Certificates.X509Certificate2]$Cert, [string]$Location)
    $store = New-Object Security.Cryptography.X509Certificates.X509Store('Root', $Location)
    $store.Open('ReadOnly')
    try {
        foreach ($existing in $store.Certificates) {
            if ($existing.Thumbprint -eq $Cert.Thumbprint) { return $true }
        }
        return $false
    }
    finally { $store.Close() }
}

function New-ContextFreeCertificate([Security.Cryptography.X509Certificates.X509Certificate2]$Cert) {
    # 从 TLS 握手链里拿到的 X509Certificate2 带着「证书上下文」，直接 X509Store.Add 会抛
    # NotSupportedException「不支持该请求」（真机实测）。用 RawData 造一份干净副本再入库。
    return [Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]]$Cert.RawData)
}

function Add-StoreRoot {
    param([Security.Cryptography.X509Certificates.X509Certificate2]$Cert, [string]$Location)
    $copy = New-ContextFreeCertificate $Cert
    $store = New-Object Security.Cryptography.X509Certificates.X509Store('Root', $Location)
    $store.Open('ReadWrite')
    try { $store.Add($copy) } finally { $store.Close() }
    return (Test-StoreTrusted -Cert $Cert -Location $Location)
}

function Remove-StoreRoot {
    # 真机实测：`Root` 的 CurrentUser 视图会把**机器库**里的证书也算进来（组策略/机器库下发的那种），
    # 这时对它 Remove 会抛「拒绝访问」。所以删除必须按 LocalMachine → CurrentUser 的顺序，
    # 并且失败只当「没删掉」，不能把整个脚本炸掉。
    param([Security.Cryptography.X509Certificates.X509Certificate2]$Cert, [string]$Location)
    try {
        $copy = New-ContextFreeCertificate $Cert
        $store = New-Object Security.Cryptography.X509Certificates.X509Store('Root', $Location)
        $store.Open('ReadWrite')
        try { $store.Remove($copy) } finally { $store.Close() }
        return $true
    }
    catch { return $false }
}

function Format-NativeArguments([string[]]$Values) {
    $parts = @()
    foreach ($value in $Values) {
        $text = [string]$value
        if ($text -match '[\s"]') { $parts += ('"' + ($text -replace '"', '\"') + '"') }
        else { $parts += $text }
    }
    return ($parts -join ' ')
}

function Invoke-NativeCapture {
    # 不用 `& wsl.exe … 2>&1`：PS 5.1 会把程序写到 stderr 的普通告警当 NativeCommandError 抛出来，
    # $ErrorActionPreference='Stop' 下直接终止。这里自己收 stdio，stderr 只当文本。
    param([string]$FilePath, [string[]]$ArgumentList = @())
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (Format-NativeArguments $ArgumentList)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $info
    try {
        [void]$proc.Start()
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()
        $code = $proc.ExitCode
    }
    finally { try { $proc.Dispose() } catch { } }
    return [pscustomobject]@{ Code = $code; Text = (([string]$stdout + [string]$stderr) -replace "`0", '') }
}

function Invoke-WslRootScript {
    # 脚本走文件、用 /mnt 路径执行：wsl.exe 的命令行还会再过一层解析，$()/$var/; 会被提前拆散。
    param([string]$Script, [string]$DistroName)
    $normalized = ($Script -replace "`r`n", "`n")
    $dir = Join-Path $env:ProgramData 'install-gateway-cert\run'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $local = Join-Path $dir ('cert-' + [guid]::NewGuid().ToString('N') + '.sh')
    [IO.File]::WriteAllText($local, $normalized, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $full = [IO.Path]::GetFullPath($local)
        $wslPath = '/mnt/' + $full.Substring(0, 1).ToLower() + ($full.Substring(2) -replace '\\', '/')
        $run = Invoke-NativeCapture -FilePath 'wsl.exe' -ArgumentList @('-d', $DistroName, '-u', 'root', '--', 'bash', '-lc', $wslPath)
        return [pscustomobject]@{ Code = $run.Code; Output = $run.Text }
    }
    finally {
        Remove-Item -LiteralPath $local -Force -ErrorAction SilentlyContinue
    }
}

function Send-EnvironmentChange {
    # SetEnvironmentVariable 只写注册表，**不会通知已运行的 Explorer**；而从开始菜单启动的
    # 桌面端继承的是 Explorer 的环境块 —— 不广播，就必须注销重登才生效。
    # WM_SETTINGCHANGE("Environment") 是官方那条通知，Explorer 收到后会重读环境。
    if (-not ('Win32.EnvBroadcast' -as [type])) {
        Add-Type -Namespace Win32 -Name EnvBroadcast -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
    }
    $result = [UIntPtr]::Zero
    [void][Win32.EnvBroadcast]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
}

function Get-DefaultPemTarget {
    # PEM 必须放在一个稳定、非临时、所有会话都能读的位置：环境变量会长期指向它。
    $root = Join-Path $env:ProgramData 'dsh-certs'
    if (-not (Test-Path -LiteralPath $root)) {
        try { New-Item -ItemType Directory -Path $root -Force | Out-Null } catch { $root = '' }
    }
    if ($root -and (Test-Path -LiteralPath $root)) { return (Join-Path $root 'gateway-root.crt') }
    # 没权限写 ProgramData（极少数锁定环境）：退回用户目录，功能一样，只是别的账号读不到。
    $fallback = Join-Path $env:LOCALAPPDATA 'dsh-certs'
    New-Item -ItemType Directory -Path $fallback -Force | Out-Null
    return (Join-Path $fallback 'gateway-root.crt')
}

function Resolve-RootCertificate {
    param([string]$TargetHost, [int]$TargetPort, [string]$LocalPem)
    if ($LocalPem) {
        if (-not (Test-Path -LiteralPath $LocalPem)) { throw "找不到 -PemPath 指定的文件：$LocalPem" }
        $loaded = New-Object Security.Cryptography.X509Certificates.X509Certificate2((Resolve-Path -LiteralPath $LocalPem).Path)
        if (-not (Test-IsCa $loaded)) { throw "-PemPath 给的不是 CA 证书：$($loaded.Subject)" }
        Write-Ok ("用本地根证书：{0}（到期 {1}）" -f $loaded.Subject, $loaded.NotAfter.ToString('yyyy-MM-dd'))
        return $loaded
    }
    $chain = (Get-TlsChain -TargetHost $TargetHost -TargetPort $TargetPort).Certs
    Write-Ok ("链上共 {0} 张证书" -f $chain.Count)
    Write-Ok ("叶证书 {0}" -f $chain[0].Subject)
    $root = $null
    foreach ($cert in $chain) { if (Test-IsCa $cert) { $root = $cert } }
    if (-not $root) {
        $chainInfo = (($chain | ForEach-Object { $_.Subject }) -join ' <- ')
        throw "证书链里没有 CA 证书（服务端下发了：$chainInfo）。请用 -PemPath 指向网关导出的根证书。"
    }
    Write-Ok ("根证书 {0}（到期 {1}）" -f $root.Subject, $root.NotAfter.ToString('yyyy-MM-dd'))
    return $root
}

function Test-DotnetTls {
    # 走 Windows 证书库的真实握手：401/403/404 都证明「TLS 通了、只是没 key」。
    param([string]$TargetHost, [int]$TargetPort, [string]$Path)
    $url = ('https://{0}:{1}{2}' -f $TargetHost, $TargetPort, $Path)
    $request = [Net.HttpWebRequest]::Create($url)
    $request.Timeout = 10000
    $request.UserAgent = 'install-gateway-cert'
    try {
        $response = $request.GetResponse()
        $code = [int]$response.StatusCode
        $response.Close()
        return ('HTTP={0}' -f $code)
    }
    catch [Net.WebException] {
        if ($_.Exception.Response) { return ('HTTP={0}' -f [int]$_.Exception.Response.StatusCode) }
        $inner = $_.Exception.InnerException
        if ($inner) { return ('ERR={0}' -f $inner.Message) }
        return ('ERR={0}' -f $_.Exception.Message)
    }
}

function Test-CurlTls {
    # schannel 走证书库；有 curl 才测（Win10 1803+ 自带）。
    # --ssl-no-revoke 必须加：内网 CA 一般不发布 CRL/OCSP，schannel 会把「查不到吊销状态」
    # 当成失败（CRYPT_E_NO_REVOCATION_CHECK），真机上 .NET 与 node 都通、只有 curl 报错。
    # 这不是信任问题，加这个开关才是 curl 在内网里的正确用法。
    param([string]$TargetHost, [int]$TargetPort, [string]$Path)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { return 'SKIP' }
    $url = ('https://{0}:{1}{2}' -f $TargetHost, $TargetPort, $Path)
    $run = Invoke-NativeCapture -FilePath $curl.Source -ArgumentList @('-sS', '--ssl-no-revoke', '-o', 'NUL', '-w', '%{http_code}', $url)
    if ($run.Text -match 'CRYPT_E_NO_REVOCATION_CHECK') { return 'HTTP=revocation-unavailable' }
    if ($run.Text -match '(?<!\d)(\d{3})(?!\d)') { return ('HTTP={0}' -f $Matches[1]) }
    return ('ERR={0}' -f ($run.Text.Trim()))
}

function Test-NodeTls {
    # Node 不读系统库：只认 NODE_EXTRA_CA_CERTS。这里用「注册表里的用户级值」去跑，
    # 而不是当前进程的环境变量 —— 要验证的正是「下次启动能不能读到」。
    param([string]$TargetHost, [int]$TargetPort, [string]$Path)
    $node = (Get-Command node.exe -ErrorAction SilentlyContinue)
    if (-not $node) { return 'SKIP' }
    $probe = Join-Path ([IO.Path]::GetTempPath()) ('cert-probe-' + [guid]::NewGuid().ToString('N') + '.js')
    $js = @'
const https = require("https");
const url = process.argv[2];
https.get(url, (r) => { console.log("NODE_HTTP=" + r.statusCode); process.exit(0); })
  .on("error", (e) => { console.log("NODE_ERR=" + (e.code || e.message)); process.exit(0); });
'@
    [IO.File]::WriteAllText($probe, $js, (New-Object System.Text.UTF8Encoding($false)))
    $saved = $env:NODE_EXTRA_CA_CERTS
    try {
        $env:NODE_EXTRA_CA_CERTS = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
        $url = ('https://{0}:{1}{2}' -f $TargetHost, $TargetPort, $Path)
        $run = Invoke-NativeCapture -FilePath $node.Source -ArgumentList @($probe, $url)
        if ($run.Text -match 'NODE_HTTP=(\d{3})') { return ('HTTP={0}' -f $Matches[1]) }
        if ($run.Text -match 'NODE_ERR=(\S+)') { return ('ERR={0}' -f $Matches[1]) }
        return ('ERR={0}' -f ($run.Text.Trim()))
    }
    finally {
        $env:NODE_EXTRA_CA_CERTS = $saved
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Main {
    $isAdmin = Test-Administrator
    if (-not $GatewayHost -and -not $PemPath) {
        throw '必须给 -GatewayHost（网关 IP/域名）或 -PemPath（本地根证书文件）：脚本不内置任何内网地址。'
    }
    if (-not $PemTarget) { $PemTarget = Get-DefaultPemTarget }

    # ---------- Uninstall ----------
    if ($Uninstall) {
        Write-Step '卸载：从证书库删除根证书 + 清掉 NODE_EXTRA_CA_CERTS'
        $pemFile = $PemTarget
        $root = $null
        if (Test-Path -LiteralPath $pemFile) {
            $root = New-Object Security.Cryptography.X509Certificates.X509Certificate2((Resolve-Path -LiteralPath $pemFile).Path)
            Write-Ok ("待删证书：{0}（指纹 {1}）" -f $root.Subject, $root.Thumbprint)
        }
        elseif ($GatewayHost) {
            $certs = (Get-TlsChain -TargetHost $GatewayHost -TargetPort $Port).Certs
            $root = @($certs | Where-Object { Test-IsCa $_ })[0]
            if ($root) { Write-Ok ("待删证书：{0}（指纹 {1}）" -f $root.Subject, $root.Thumbprint) }
        }
        if (-not $root) { throw "既没有 PEM（$pemFile）也抓不到链，不知道该删哪张证书。" }

        $problems = 0
        $listedByMachine = Test-StoreTrusted -Cert $root -Location 'LocalMachine'
        if ($isAdmin) {
            if (Test-StoreTrusted -Cert $root -Location 'LocalMachine') {
                if (Remove-StoreRoot -Cert $root -Location 'LocalMachine') { Write-Ok '已从机器库 LocalMachine\Root 删除' }
                else { Write-Err 'LocalMachine\Root 删除失败'; $problems++ }
            }
        }
        elseif ($listedByMachine) {
            Write-Warn2 '证书在机器库 LocalMachine\Root 里：删它需要管理员（用管理员 PowerShell 重跑 -Uninstall）'
            $problems++
        }
        # 机器库删完后，用户视图里通常就看不到它了；仍在的话再删用户库。
        if (Test-StoreTrusted -Cert $root -Location 'CurrentUser') {
            if (Remove-StoreRoot -Cert $root -Location 'CurrentUser') { Write-Ok '已从用户库 CurrentUser\Root 删除' }
            elseif ($problems -eq 0) { Write-Err 'CurrentUser\Root 删除失败（被组策略/另一个账号锁定？）'; $problems++ }
        }
        if ([Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')) {
            [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $null, 'User')
            Write-Ok '已清掉用户级 NODE_EXTRA_CA_CERTS'
        }
        if (Test-Path -LiteralPath $pemFile) { Remove-Item -LiteralPath $pemFile -Force; Write-Ok ("已删除 PEM：{0}" -f $pemFile) }
        $stillMachine = Test-StoreTrusted -Cert $root -Location 'LocalMachine'
        $stillUser = Test-StoreTrusted -Cert $root -Location 'CurrentUser'
        if ($stillMachine -or $stillUser) { $problems = [Math]::Max($problems, 1) }
        Write-Ok ("卸载后：LocalMachine={0} CurrentUser={1}" -f $(if ($stillMachine) { '仍在' } else { '已无' }), $(if ($stillUser) { '仍在' } else { '已无' }))
        Write-Host ("CERT_RESULT={0}" -f $(if ($problems -gt 0) { 'problems' } else { 'uninstalled' }))
        return $(if ($problems -gt 0) { 7 } else { 0 })
    }

    # ---------- 1) 取根证书 ----------
    Write-Step "取根证书：$(if ($PemPath) { $PemPath } else { "https://${GatewayHost}:${Port}/" })"
    $root = Resolve-RootCertificate -TargetHost $GatewayHost -TargetPort $Port -LocalPem $PemPath
    Write-Ok ("根指纹 {0}" -f $root.Thumbprint)
    $trustedUser = Test-StoreTrusted -Cert $root -Location 'CurrentUser'
    $trustedMachine = $false
    if ($isAdmin) { $trustedMachine = Test-StoreTrusted -Cert $root -Location 'LocalMachine' }
    Write-Ok ("安装前：CurrentUser={0} LocalMachine={1}" -f $(if ($trustedUser) { '已信任' } else { '未信任' }), $(if (-not $isAdmin) { '需管理员' } elseif ($trustedMachine) { '已信任' } else { '未信任' }))

    if ($DryRun) {
        Write-Step 'DryRun：只抓链，不做任何修改'
        Write-Host ("ROOT_THUMB={0}" -f $root.Thumbprint)
        Write-Host ("ROOT_SUBJECT={0}" -f $root.Subject)
        Write-Host ("ROOT_EXPIRES={0}" -f $root.NotAfter.ToString('yyyy-MM-dd'))
        Write-Host ("CURRENTUSER_TRUSTED={0}" -f $trustedUser)
        if ($GatewayHost) {
            Write-Host ("DOTNET_TLS_BEFORE={0}" -f (Test-DotnetTls -TargetHost $GatewayHost -TargetPort $Port -Path $ProbePath))
            Write-Host ("CURL_TLS_BEFORE={0}" -f (Test-CurlTls -TargetHost $GatewayHost -TargetPort $Port -Path $ProbePath))
            Write-Host ("NODE_TLS_BEFORE={0}" -f (Test-NodeTls -TargetHost $GatewayHost -TargetPort $Port -Path $ProbePath))
        }
        return 0
    }

    $problems = 0

    # ---------- 2) 机器证书库（管理员；这是权威锚点，所有账号与后台服务都看它）----------
    $trustedNow = $trustedUser -or $trustedMachine
    Write-Step '1/3 机器证书库 Cert:\LocalMachine\Root'
    if (-not $isAdmin) {
        Write-Warn2 '当前不是管理员：跳过机器库，改用用户库（本账号下的桌面端同样能读到）'
        Write-Warn2 '想装成「所有用户都信任」（推荐）：用管理员 PowerShell 重跑一次本脚本'
    }
    elseif ($trustedMachine) { Write-Ok '已信任：不重复导入' }
    elseif (Add-StoreRoot -Cert $root -Location 'LocalMachine') { Write-Ok '已导入'; $trustedNow = $true }
    else { Write-Err '机器库导入后仍查不到：证书库拒绝写入（组策略锁定了根证书库？）'; $problems++ }

    # ---------- 3) 用户证书库（非管理员的唯一路径）----------
    Write-Step '2/3 用户证书库 Cert:\CurrentUser\Root'
    if ($trustedNow) { Write-Ok '机器库已信任，用户库无需重复导入' }
    elseif ($trustedUser) { Write-Ok '已信任：不重复导入' }
    elseif (Add-StoreRoot -Cert $root -Location 'CurrentUser') { Write-Ok '已导入'; $trustedNow = $true }
    else {
        # 真机实测：非交互会话里往 CurrentUser\Root 写会返回「不支持该请求」（ERROR_NOT_SUPPORTED）。
        Write-Err '用户库也写不进去（ERROR_NOT_SUPPORTED）：请用**管理员** PowerShell 重跑本脚本（装机器库）。'
        $problems++
    }

    # ---------- 4) PEM + NODE_EXTRA_CA_CERTS ----------
    Write-Step ("3/3 Node 用的 PEM + NODE_EXTRA_CA_CERTS：{0}" -f $PemTarget)
    $pemDir = Split-Path -Parent $PemTarget
    if ($pemDir -and -not (Test-Path -LiteralPath $pemDir)) { New-Item -ItemType Directory -Path $pemDir -Force | Out-Null }
    [IO.File]::WriteAllText($PemTarget, (ConvertTo-Pem $root), (New-Object System.Text.UTF8Encoding($false)))
    if (-not (Test-Path -LiteralPath $PemTarget)) {
        Write-Err 'PEM 没写出来：node 会报 SELF_SIGNED_CERT_IN_CHAIN'
        $problems++
    }
    else {
        Write-Ok ("已写 {0} 字节" -f (Get-Item -LiteralPath $PemTarget).Length)
        [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $PemTarget, 'User')
        $persisted = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
        if ($persisted -eq $PemTarget) { Write-Ok ("用户级 NODE_EXTRA_CA_CERTS → {0}" -f $persisted) }
        else { Write-Err ("环境变量没写进注册表（读到「{0}」）" -f $persisted); $problems++ }
        # 通知 Explorer：否则开始菜单启动的桌面端拿到的还是旧环境（要注销重登才行）。
        try { Send-EnvironmentChange; Write-Ok '已通知 Explorer 重读环境变量（不必注销重登）' }
        catch { Write-Warn2 ("通知 Explorer 失败（{0}）：桌面端需要注销后重新登录才读到新变量" -f $_.Exception.Message) }
    }

    # ---------- 5) 验证 ----------
    if ($GatewayHost) {
        Write-Step "验证：https://${GatewayHost}:${Port}${ProbePath}"
        $dotnet = Test-DotnetTls -TargetHost $GatewayHost -TargetPort $Port -Path $ProbePath
        $curl = Test-CurlTls -TargetHost $GatewayHost -TargetPort $Port -Path $ProbePath
        $node = Test-NodeTls -TargetHost $GatewayHost -TargetPort $Port -Path $ProbePath
        foreach ($pair in @(@('Windows 证书库（.NET/schannel/Chromium）', $dotnet), @('curl.exe', $curl), @('node（NODE_EXTRA_CA_CERTS）', $node))) {
            $label = $pair[0]; $value = $pair[1]
            if ($value -eq 'SKIP') { Write-Warn2 ("{0}：这台机器没有，跳过" -f $label); continue }
            if ($value -match '^HTTP=(200|204|301|302|400|401|403|404|revocation-unavailable)$') { Write-Ok ("{0}：{1}（TLS 通了）" -f $label, $value) }
            else { Write-Err ("{0}：{1}" -f $label, $value); $problems++ }
        }
        Write-Host ("DOTNET_TLS={0}" -f $dotnet)
        Write-Host ("CURL_TLS={0}" -f $curl)
        Write-Host ("NODE_TLS={0}" -f $node)
    }

    # ---------- 6) 可选：WSL ----------
    if ($WithWsl) {
        Write-Step ("额外：WSL（{0}）ca-certificates" -f $Distro)
        $pemForWsl = Join-Path ([IO.Path]::GetTempPath()) ('gateway-root-' + [guid]::NewGuid().ToString('N') + '.pem')
        [IO.File]::WriteAllText($pemForWsl, (ConvertTo-Pem $root), (New-Object System.Text.UTF8Encoding($false)))
        $full = [IO.Path]::GetFullPath($pemForWsl)
        $wslPem = '/mnt/' + $full.Substring(0, 1).ToLower() + ($full.Substring(2) -replace '\\', '/')
        $script = @"
set -eu
install -D -m 0644 "$wslPem" "/usr/local/share/ca-certificates/gateway-root.crt"
update-ca-certificates >/dev/null 2>&1
curl -fsS -o /dev/null "https://$GatewayHost" && echo WSL_TLS=ok || echo WSL_TLS=failed
"@
        $wslResult = Invoke-WslRootScript -Script $script -DistroName $Distro
        Remove-Item -LiteralPath $pemForWsl -Force -ErrorAction SilentlyContinue
        Write-Host $wslResult.Output
        if ($wslResult.Output -match 'WSL_TLS=ok') { Write-Ok 'WSL 侧 TLS 通过（curl）' }
        else { Write-Err 'WSL 里装完仍连不通：在 WSL 里跑 curl -v 看是哪一层拒绝'; $problems++ }
    }

    # ---------- 7) 收尾 ----------
    Write-Step '回执'
    Write-Host ("CERT_ROOT_THUMB={0}" -f $root.Thumbprint)
    Write-Host ("CERT_PEM={0}" -f $PemTarget)
    Write-Host ("CERT_NODE_ENV={0}" -f ([Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')))
    Write-Host ("CERT_RESULT={0}" -f $(if ($problems -gt 0) { 'problems' } else { 'ok' }))
    if ($problems -gt 0) {
        Write-Warn2 '有检查没通过：把上面整段输出发回去，别自己猜。'
        return 7
    }
    Write-Host ''
    Write-Ok '证书装好了。'
    Write-Step 'DSH 桌面端（DeepSeek Desktop）下一步'
    Write-Host '  桌面端是 Electron：窗口里的网络走 Windows 证书库，Node 侧只认 NODE_EXTRA_CA_CERTS，两处都已就位。'
    Write-Host '  但环境变量只影响**新启动**的进程，所以：'
    Write-Host '    1) 完全退出桌面端（托盘/任务栏退出，不是最小化），再重新打开；'
    Write-Host '    2) 若还报 self-signed / SELF_SIGNED_CERT_IN_CHAIN：注销 Windows 再登录（Explorer 缓存了旧环境），'
    Write-Host '       或从「开始菜单 → Windows PowerShell」里启动桌面端验证；'
    Write-Host ('    3) 还不行就把桌面端日志和环境变量一起发回：echo $env:NODE_EXTRA_CA_CERTS 应等于 {0}' -f $PemTarget)
    return 0
}

# 只有真跑（不是被 dot-source）时才执行主流程：测试要能 dot-source 进来单测纯函数。
if ($MyInvocation.InvocationName -ne '.') {
    try {
        exit (Invoke-Main)
    }
    catch {
        Write-Err ("证书安装中止：{0}" -f $_.Exception.Message)
        Write-Warn2 '这不是"差不多能用"的问题：网关证书没装上，DSH 之后每次 HTTPS 都会失败。'
        exit 1
    }
}
