#Requires -Version 5.1
<#
编译通过不等于能跑。Inno 的 Pascal 脚本是运行时解释的：类型不匹配、属性不存在这类错误
ISCC 不报，只有真的跑起来才炸。本仓就踩过一次——`TInputQueryWizardPage.Add('…', '默认值')`
编译通过，双击 exe 一开就 "Type Mismatch" 崩掉。

所以每次改完 .iss 都要跑这个冒烟：
  1. 用 SYSTEM 计划任务启动 setup.exe /SILENT /SUPPRESSMSGBOXES /NOCANCEL /LOG=…
     （SSH 会话启动的进程没有提权令牌，/SILENT 会卡在看不见的 UAC 框上）
  2. 读日志：必须出现 "Log opened"，且不出现 Runtime error / Type Mismatch / Internal error
  3. 空配置跑必然停在参数校验（"Linux 密码至少 8 位"）——那里弹框会一直等着，
     所以「30 秒内到达校验点且没报错」就是通过；真装完反而说明校验没生效。

用法：
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows\installer\smoke-test.ps1 `
      -Exe C:\Users\demo\build-mini\out\setup.exe
#>
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

if (-not (Test-Path -LiteralPath $Exe)) { Write-Host "[fail] 找不到 exe：$Exe" -ForegroundColor Red; exit 1 }
$exe = (Resolve-Path -LiteralPath $Exe).Path
$log = Join-Path $env:TEMP ('feishu-kit-smoke-' + [guid]::NewGuid().ToString('N') + '.log')
$done = "$log.done"
$taskBat = Join-Path $env:TEMP ('feishu-kit-smoke-' + [guid]::NewGuid().ToString('N') + '.cmd')
$taskName = 'FeishuKitSmokeTest'

@(
    '@echo off',
    ('"' + $exe + '" /SILENT /SUPPRESSMSGBOXES /NOCANCEL /LOG=' + $log),
    ('echo EXIT=%ERRORLEVEL% > "' + $done + '"')
) | Set-Content -LiteralPath $taskBat -Encoding ASCII

Write-Host ("[1/3] 以 SYSTEM 身份启动：" + $exe)
& schtasks.exe /Create /TN $taskName /TR "`"$taskBat`"" /SC ONCE /ST 00:00 /RL HIGHEST /RU SYSTEM /F | Out-Null
& schtasks.exe /Run /TN $taskName | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $done)) { Start-Sleep -Milliseconds 500 }
$finished = Test-Path -LiteralPath $done

# 产物名统一叫 setup.exe，按名字杀会误杀别人的安装程序：只杀本次 $Exe 起的那个
Get-CimInstance Win32_Process -Filter "Name LIKE 'setup%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $Exe -or $_.CommandLine -like ('*' + $Exe + '*') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
& schtasks.exe /Delete /TN $taskName /F | Out-Null

if (-not (Test-Path -LiteralPath $log)) {
    Write-Host "[fail] setup 没写出日志。两种可能：" -ForegroundColor Red
    Write-Host '  ① 启动阶段就崩了（连日志都没来得及建）—— 这是真失败；'
    Write-Host ('  ② 超时太短：大包启动时要先把自身复制进临时目录，778 MB 那份要几分钟。-TimeoutSec 现在是 ' + $TimeoutSec + '。')
    Write-Host '  判据：拿 5 MB 的 mini 包冒烟（同样的 [Code]，秒级）；全量包请直接人工双击验。'
    exit 1
}

$text = Get-Content -LiteralPath $log -Raw -Encoding UTF8
Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $done -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $taskBat -Force -ErrorAction SilentlyContinue

$fatal = @('Runtime error', 'Type Mismatch', 'Internal error', 'Runtime Error')
$hits = @()
foreach ($needle in $fatal) { if ($text -match [regex]::Escape($needle)) { $hits += $needle } }
if ($hits.Count -gt 0) {
    Write-Host ("[fail] 日志里有运行时错误：" + ($hits -join '，')) -ForegroundColor Red
    Write-Host '--- 日志尾部 ---'
    $text -split "`r?`n" | Select-Object -Last 20 | ForEach-Object { Write-Host ('  ' + $_) }
    exit 1
}

Write-Host ('[2/3] 日志正常（' + (($text -split "`r?`n").Count) + ' 行），没有运行时错误')

$reachedValidation = $text -match 'Linux 密码至少 8 位'
$completed = $text -match 'Installation process succeeded|安装完成'
if ($reachedValidation) {
    Write-Host '[3/3] 已到达参数校验（空配置的预期停点）→ 冒烟通过'
    Write-Host 'SMOKE_TEST ok=1 reason=reached_validation'
    exit 0
}
if ($completed) {
    Write-Host '[3/3] 竟然装完了（配置来自默认值？）→ 冒烟通过'
    Write-Host 'SMOKE_TEST ok=1 reason=completed'
    exit 0
}
Write-Host ('[3/3] 既没到校验点也没装完（finished=' + $finished + '）→ 判失败') -ForegroundColor Red
Write-Host '--- 日志尾部 ---'
$text -split "`r?`n" | Select-Object -Last 20 | ForEach-Object { Write-Host ('  ' + $_) }
Write-Host 'SMOKE_TEST ok=0 reason=no_progress'
exit 1
