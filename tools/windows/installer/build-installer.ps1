#Requires -Version 5.1
<#
.SYNOPSIS
  把 setup.exe 打出来：拼载荷 → 调 ISCC → 报 exe 路径和 sha256。

.DESCRIPTION
  ISCC 只有 Windows 版，所以这个构建脚本是 PowerShell（必须在装了 Inno Setup 6 的机器上跑）。
  载荷分两块，安装后在 {app}\offline 下重组成离线包的目录形状（repo/ + payload/）：

    payload\repo\     仓库快照（dist/、tools/、docs/…）—— 安装时被镜像进 WSL；
    payload\payload\  离线材料（ubuntu/*.wsl、node、npm、apt、windows/*.msi）—— 前置安装用。

  内网参数（网关地址/渠道/key 默认值）从 -ConfigJson 读，**不进仓库**：
    { "GatewayHost": "...", "GatewayBaseUrl": "...", "Provider": "...", "ChannelVar": "...",
      "DshModel": "...", "DshVersion": "0.2.2-rc.1", "DistroName": "Ubuntu",
      "DefaultLinuxUser": "", "ApiKeyDefault": "", "AdminPubKeyDefault": "",
      "AppVersion": "1.0.0" }
    DefaultLinuxUser 留空 = 向导按装机的 Windows 用户名推导（别写死某个人的名字，包会发给别人）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build-installer.ps1 `
      -RepoRoot C:\Users\demo\repo-current `
      -PayloadSource C:\Users\demo\feishu-bridge-win-prereq-20261104\payload `
      -ConfigJson C:\Users\demo\installer-config.json

.NOTES
  退出码：0 成功；2 参数/环境问题；6 载荷缺失；7 ISCC 编译失败。
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    # 发布构建：用 git archive 打出来的 zip。**推荐**，因为它只含仓库里跟踪的文件，
    # 不会把 docs/windows-hosts.csv / 本地配置这类 gitignore 的本机文件打进 exe。
    [string]$RepoArchive = '',
    [Parameter(Mandatory = $true)][string]$PayloadSource,
    [Parameter(Mandatory = $true)][string]$ConfigJson,
    [string]$OutDir = '',
    [string]$IsccPath = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Write-Step([string]$Text) { Write-Host ''; Write-Host "=== $Text ===" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "  [warn] $Text" -ForegroundColor Yellow }

if (-not $RepoArchive) {
    if (-not $RepoRoot) {
        $here = $PSScriptRoot
        # dist/windows/installer → 仓库根
        $RepoRoot = (Resolve-Path (Join-Path $here '..\..\..')).Path
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'dist\wsl2\install-windows.ps1'))) {
        Write-Host "[fail] RepoRoot 不像仓库根（找不到 dist\wsl2\install-windows.ps1）：$RepoRoot" -ForegroundColor Red
        exit 2
    }
    Write-Warn2 '用 -RepoRoot 工作树打包：本机 gitignore 的私有文件也会被装进去（只适合自测，发布请用 -RepoArchive）'
}
if (-not (Test-Path -LiteralPath $PayloadSource)) { Write-Host "[fail] 载荷源不存在：$PayloadSource" -ForegroundColor Red; exit 6 }

if (-not $IsccPath) {
    $candidates = @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate) { $IsccPath = $candidate; break } }
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) {
    Write-Host '[fail] 找不到 ISCC.exe：装 Inno Setup 6，或用 -IsccPath 指定' -ForegroundColor Red
    exit 2
}

if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot 'build' }
$config = Get-Content -LiteralPath $ConfigJson -Raw -Encoding UTF8 | ConvertFrom-Json

Write-Step "1/4 准备构建目录 $OutDir"
if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
$stage = Join-Path $OutDir 'payload'
New-Item -ItemType Directory -Path (Join-Path $stage 'repo') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'payload') -Force | Out-Null

Write-Step '2/4 仓库快照 → payload\repo'
if ($RepoArchive) {
    # git archive 已经把 .gitignore 的东西排除干净了，直接展开即可。
    Expand-Archive -LiteralPath $RepoArchive -DestinationPath (Join-Path $stage 'repo') -Force
    Write-Ok ('repo 快照来自 git archive：' + [IO.Path]::GetFileName($RepoArchive))
}
else {
    # 用 robocopy 而不是 Copy-Item：要排除 .git / node_modules / 本机私有目录，且要快。
    $excludeDirs = @('.git', 'node_modules', '__pycache__', '.venv', '.pytest_cache', 'reviews', '.codegraph')
    $excludeFiles = @('.DS_Store', '._.DS_Store', '*.pyc')
    $robocopyArgs = @($RepoRoot, (Join-Path $stage 'repo'), '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/R:1', '/W:1')
    foreach ($dir in $excludeDirs) { $robocopyArgs += @('/XD', (Join-Path $RepoRoot $dir)) }
    foreach ($file in $excludeFiles) { $robocopyArgs += @('/XF', $file) }
    & robocopy.exe @robocopyArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Host "[fail] robocopy 失败：$LASTEXITCODE" -ForegroundColor Red; exit 6 }
}
Write-Ok ("repo 快照 {0:N0} 个文件" -f (Get-ChildItem (Join-Path $stage 'repo') -Recurse -File).Count)

Write-Step '3/4 离线材料 → payload\payload'
& robocopy.exe $PayloadSource (Join-Path $stage 'payload') /E /NFL /NDL /NJH /NJS /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "[fail] robocopy 载荷失败：$LASTEXITCODE" -ForegroundColor Red; exit 6 }
$applySource = Join-Path $PSScriptRoot 'apply.ps1'
if (-not (Test-Path -LiteralPath $applySource)) { $applySource = Join-Path $stage 'repo\dist\windows\installer\apply.ps1' }
Copy-Item -LiteralPath $applySource -Destination (Join-Path $OutDir 'apply.ps1') -Force
Write-Ok ("载荷 {0:N0} 个文件 / {1:N0} MB" -f (Get-ChildItem (Join-Path $stage 'payload') -Recurse -File).Count, ((Get-ChildItem (Join-Path $stage 'payload') -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

Write-Step '4/4 ISCC 编译'
$defines = @{
    AppVersion         = $(if ($config.AppVersion) { $config.AppVersion } else { '1.0.0' })
    GatewayHost        = [string]$config.GatewayHost
    GatewayBaseUrl     = [string]$config.GatewayBaseUrl
    Provider           = [string]$config.Provider
    ChannelVar         = [string]$config.ChannelVar
    DshModel           = [string]$config.DshModel
    DshVersion         = $(if ($config.DshVersion) { $config.DshVersion } else { '0.2.2-rc.1' })
    DistroName         = $(if ($config.DistroName) { $config.DistroName } else { 'Ubuntu' })
    # 空 = 让 .iss 按 Windows 用户名推导（默认值写死某个人的名字会跟着包发给别人）
    DefaultLinuxUser   = $(if ($config.DefaultLinuxUser) { $config.DefaultLinuxUser } else { '' })
    ApiKeyDefault      = [string]$config.ApiKeyDefault
    AdminPubKeyDefault = [string]$config.AdminPubKeyDefault
    StageDir           = $OutDir
    OutputDir          = (Join-Path $OutDir 'out')
}
foreach ($name in @('GatewayHost', 'GatewayBaseUrl', 'Provider', 'ChannelVar', 'DshModel')) {
    if (-not $defines[$name]) { Write-Warn2 "配置里 $name 是空的：装到那一步会失败，确认一下 $ConfigJson" }
}
$issArguments = @('/Qp')
foreach ($name in $defines.Keys) { $issArguments += ('/D{0}={1}' -f $name, $defines[$name]) }
$issArguments += (Join-Path $PSScriptRoot 'feishu-kit.iss')

$isccLog = Join-Path $OutDir 'iscc.log'
Push-Location $PSScriptRoot
try {
    # 把 ISCC 的 stdout/stderr 全收进文件再整段打印：PS 5.1 在管道里遇到 stderr 会包成
    # NativeCommandError 并截断后面的报错，编译错误正好都在后面几行。
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $IsccPath
    $process.StartInfo.Arguments = ($issArguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $isccCode = $process.ExitCode
    $process.Dispose()
    ($stdout + "`n" + $stderr) | Set-Content -LiteralPath $isccLog -Encoding UTF8
    Get-Content -LiteralPath $isccLog | ForEach-Object { Write-Host ('  ' + $_) }
}
finally { Pop-Location }
if ($isccCode -ne 0) { Write-Host "[fail] ISCC 退出码 $isccCode" -ForegroundColor Red; exit 7 }

$exe = Join-Path (Join-Path $OutDir 'out') 'setup.exe'
if (-not (Test-Path -LiteralPath $exe)) { Write-Host "[fail] 没找到产物：$exe" -ForegroundColor Red; exit 7 }
$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item -LiteralPath $exe).Length
Write-Host ''
Write-Ok ('setup.exe 已生成：{0}' -f $exe)
Write-Host ('  {0:N0} MB  sha256 {1}' -f ($size / 1MB), $hash) -ForegroundColor Green
Write-Host ('  INSTALLER_EXE={0}' -f $exe)
Write-Host ('  INSTALLER_SHA256={0}' -f $hash)
exit 0
