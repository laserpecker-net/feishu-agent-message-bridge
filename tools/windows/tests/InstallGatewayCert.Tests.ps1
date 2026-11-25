#Requires -Version 5.1
<#
tools/windows/install-gateway-cert.ps1 的测试。跑法：
  pwsh -NoProfile -File tools/windows/tests/InstallGatewayCert.Tests.ps1      # POSIX
  powershell -NoProfile -File tools\windows\tests\InstallGatewayCert.Tests.ps1 # Windows

只测纯函数与静态契约：抓链/装库需要真网关（真机判据见 docs/HISTORY.md「证书」一节）。
#>
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$scriptPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'windows/install-gateway-cert.ps1'
if (-not (Test-Path $scriptPath)) { $scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'install-gateway-cert.ps1' }
if (-not (Test-Path $scriptPath)) { $scriptPath = Join-Path $PSScriptRoot 'install-gateway-cert.ps1' }
if (-not (Test-Path $scriptPath)) { throw "找不到 install-gateway-cert.ps1：$scriptPath" }
. $scriptPath
$source = Get-Content -LiteralPath $scriptPath -Raw

$script:failures = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { $script:failures++; Write-Host "FAIL: $Message" -ForegroundColor Red }
    else { Write-Host "ok: $Message" }
}
function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ($Expected -ne $Actual) { Write-Host ("  expected: {0}`n  actual:   {1}" -f $Expected, $Actual) -ForegroundColor Yellow }
    Assert-True ($Expected -eq $Actual) $Message
}

# --- Test-IsCa：只看 BasicConstraints.CertificateAuthority，不猜名字 ---
# 自签 CA 必须显式写 BasicConstraints ca=1：只给 -KeyUsage CertSign 生成出来的证书是 CA:FALSE，
# 于是 Test-IsCa 判定成叶证书 —— 这是测试夹具错，不是函数错。
$caCert = New-SelfSignedCertificate -DnsName 'feishu-test-ca' -CertStoreLocation 'Cert:\CurrentUser\My' `
    -KeyUsage CertSign, CRLSign -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddDays(2) `
    -TextExtension @('2.5.29.19={critical}{text}ca=1')
$leafCert = New-SelfSignedCertificate -DnsName 'feishu-test-leaf' -CertStoreLocation 'Cert:\CurrentUser\My' `
    -Signer $caCert -NotAfter (Get-Date).AddDays(1)
try {
    Assert-True (Test-IsCa $caCert) 'CA:TRUE 证书被识别为 CA'
    Assert-True (-not (Test-IsCa $leafCert)) '叶证书不被识别为 CA'

    # --- ConvertTo-Pem：形状对了 Node/OpenSSL 才认 ---
    $pem = ConvertTo-Pem $caCert
    $lines = $pem -split "`n" | Where-Object { $_ }
    Assert-Equal '-----BEGIN CERTIFICATE-----' $lines[0] 'PEM 以 BEGIN 行开头'
    Assert-Equal '-----END CERTIFICATE-----' $lines[-1] 'PEM 以 END 行结尾'
    Assert-Equal 64 $lines[1].Length '每行 base64 宽 64（OpenSSL 习惯）'
    Assert-True ($pem -match "`n$") 'PEM 以换行结尾（否则 ca-certificates 会报 no newline）'
    $reimported = New-Object Security.Cryptography.X509Certificates.X509Certificate2
    $reimported.Import([Text.Encoding]::ASCII.GetBytes($pem))
    Assert-Equal $caCert.Thumbprint $reimported.Thumbprint 'PEM 往返后指纹不变'
}
finally {
    Remove-Item -Path ("Cert:\CurrentUser\My\" + $caCert.Thumbprint) -Force -ErrorAction SilentlyContinue
    Remove-Item -Path ("Cert:\CurrentUser\My\" + $leafCert.Thumbprint) -Force -ErrorAction SilentlyContinue
}

# --- Format-NativeArguments：路径带空格时必须整体加引号 ---
Assert-Equal 'wsl.exe -d Ubuntu' (Format-NativeArguments @('wsl.exe', '-d', 'Ubuntu')) '普通参数不加引号'
Assert-Equal '"C:\Program Files\x.sh"' (Format-NativeArguments @('C:\Program Files\x.sh')) '带空格的参数整体加引号'

# --- 静态契约：Windows 上「信任一张自签 CA」必须两处都有名字，缺一处就会一半能通 ---
# 断言一律用单引号字符串 + Contains：双引号里 \$ 不是转义，$变量 会被替换掉（真机踩过）。
Assert-True ($source.Contains('X509Store(''Root'', $Location)')) '机器库与用户库走同一个实现（不是一个库一套代码）'
Assert-True ($source.Contains('''LocalMachine''')) '机器证书库是权威锚点'
Assert-True ($source.Contains('''CurrentUser''')) '用户证书库是非管理员路径'
Assert-True ($source.Contains('SetEnvironmentVariable(''NODE_EXTRA_CA_CERTS'', $PemTarget, ''User'')')) 'Node 走用户级 NODE_EXTRA_CA_CERTS（Node 不读系统库）'
Assert-True ($source.Contains('GetEnvironmentVariable(''NODE_EXTRA_CA_CERTS'', ''User'')')) '验证读注册表里的值，而不是当前进程的'
Assert-True ($source.Contains('/v1/models')) '默认探测路径是 /v1/models'
Assert-True ($source.Contains('[switch]$WithWsl')) 'WSL 是可选开关（只用 Windows 桌面端时不需要）'
Assert-True ($source.Contains('/usr/local/share/ca-certificates')) 'WSL 侧装进 ca-certificates'
Assert-True (-not ($source -match '\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')) '脚本里不出现任何 IP（内网地址只能在 .gitignore 的本地配置里）'
Assert-True (-not ($source -match 'feishu-kit|feishu-bridge')) '脚本不依赖安装包目录/桥接目录（独立可用）'
Assert-True ($source.Contains('InvocationName -ne')) '支持 dot-source（测试可单测纯函数）'

# --- 桌面端（Electron）专属契约 ---
Assert-True ($source.Contains('SendMessageTimeout')) '设完环境变量广播 WM_SETTINGCHANGE（Explorer 才会重读）'
Assert-True ($source.Contains('function Send-EnvironmentChange')) '广播是一个函数（不是散落在主流程里）'
Assert-True ($source -match 'Desktop|桌面端') '明确说明桌面端怎么生效（退出重开 / 注销）'

# --- curl 的 schannel 必须关掉吊销检查：内网 CA 不发布 CRL，真机报 CRYPT_E_NO_REVOCATION_CHECK ---
Assert-True ($source.Contains('--ssl-no-revoke')) 'curl 探测带 --ssl-no-revoke（内网 CA 无 CRL）'
Assert-True ($source.Contains('revocation-unavailable')) '「查不到吊销状态」判为 TLS 通过，不算失败'

# --- 从 TLS 链拿到的证书带着上下文，直接入库会 NotSupportedException「不支持该请求」 ---
Assert-True ($source.Contains('function New-ContextFreeCertificate')) '入库前用 RawData 造干净副本'

# --- 双击启动器（.cmd）：公司机器上「禁止运行脚本」的解法 ---
# 真机：PowerShell 默认 Restricted → `-File xxx.ps1` 报「因为在此系统上禁止运行脚本」；
# 组策略锁成 AllSigned 时 `-ExecutionPolicy Bypass` 也会被覆盖。所以启动器把脚本读进内存执行。
$cmdPath = Join-Path (Split-Path -Parent $scriptPath) 'install-gateway-cert.cmd'
Assert-True (Test-Path -LiteralPath $cmdPath) ('启动器存在：' + $cmdPath)
if (Test-Path -LiteralPath $cmdPath) {
    $cmdText = (Get-Content -LiteralPath $cmdPath -Raw -Encoding Default)
    Assert-True ($cmdText -match 'scriptblock') '启动器用 scriptblock 读进内存执行（不受执行策略限制）'
    Assert-True ($cmdText -match 'net session') '启动器自己检查管理员（证书要进机器库）'
    Assert-True ($cmdText -match 'pause') '启动器结束前停窗（同事要能看到结果）'
    Assert-True ($cmdText -match 'set /p "GW=') '没给参数时启动器会问网关地址'
    Assert-True ($cmdText -notmatch '192\.168\.') '启动器里不写死任何内网地址'
}

# --- 抓链必须在回调里取证书：同一 X509Chain 实例握手后会被清空（真机实测） ---
Assert-True ($source -match 'foreach \(\$element in @\(\$chain\.ChainElements\)\)') '回调内部取 ChainElements'
Assert-True ($source -match 'ServicePointManager\]::ServerCertificateValidationCallback') '用 HttpWebRequest 的全局回调抓链（SslStream 的链是空的）'

if ($script:failures -gt 0) { Write-Host "$($script:failures) test(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'ALL INSTALL-GATEWAY-CERT TESTS PASSED'
exit 0
