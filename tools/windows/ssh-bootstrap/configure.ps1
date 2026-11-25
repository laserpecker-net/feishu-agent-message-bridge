# =====================================================================
#  Windows SSH bootstrap - configuration step  (fleet edition v1.1)
#
#  Called by install.bat after the OpenSSH Server MSI is present.
#  Standalone use (as Administrator):
#      powershell -NoProfile -ExecutionPolicy Bypass -File configure.ps1
#
#  Design notes for fleet use:
#    * Always installs keys into
#          C:\ProgramData\ssh\administrators_authorized_keys
#      This file is read for ALL administrator accounts, so it does not
#      matter which admin account runs this script.
#    * Idempotent. Never removes existing keys.
#    * Result file goes to a configurable directory (settings.txt
#      result_dir=...), falling back to the package folder, then %TEMP%.
#      NO assumption that any network share is reachable.
#    * Prints a one-line SUMMARY that is easy to copy back over chat.
#    * ASCII only (PS 5.1 reads BOM-less non-ASCII using the ANSI code page).
# =====================================================================

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $root) { $root = (Get-Location).Path }

$logDir = $env:TEMP
if (-not $logDir) { $logDir = $root }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $logDir ("ssh-bootstrap-$stamp.log")

try { Start-Transcript -Path $log -Force | Out-Null } catch { }

$script:ok = $true
$script:notes = @()

function Note([string]$m) { $script:notes += $m; Write-Host "    $m" }
function Problem([string]$m) { $script:ok = $false; $script:notes += "PROBLEM: $m"; Write-Host "[X] $m" -ForegroundColor Red }

# ---------------------------------------------------------------- result dir
# Priority: settings.txt result_dir=  ->  package folder  ->  %TEMP%
$resultDir = $root
$settingsFile = Join-Path $root 'settings.txt'
if (Test-Path -LiteralPath $settingsFile) {
    foreach ($line in (Get-Content -LiteralPath $settingsFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*result_dir\s*=\s*(.+?)\s*$') {
            $candidate = $matches[1].Trim('"').Trim("'").Trim()
            if ($candidate) { $resultDir = $candidate }
        }
    }
}

function Write-Result([string]$text) {
    foreach ($dir in @($resultDir, $root, $logDir)) {
        if (-not $dir) { continue }
        try {
            if (-not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
            }
            $f = Join-Path $dir ("result-" + $env:COMPUTERNAME + ".txt")
            Set-Content -LiteralPath $f -Value $text -Encoding ascii -ErrorAction Stop
            return $f
        } catch { }
    }
    return ''
}

function Finish {
    Write-Host ""
    Write-Host "================ RESULT ================"
    $addrs = @()
    try {
        $addrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
                   Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
                   Select-Object -ExpandProperty IPAddress)
    } catch { }

    $svcStatus = 'MISSING'
    try { $svcStatus = (Get-Service sshd -ErrorAction Stop).Status } catch { }

    $listenPorts = @()
    try { $listenPorts = @(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction Stop) } catch { }
    $listen = [bool]($listenPorts.Count -gt 0)

    $verdict = 'OK'
    if (-not $script:ok -or $svcStatus -ne 'Running' -or -not $listen) { $verdict = 'FAIL' }

    Write-Host "Hostname : $env:COMPUTERNAME"
    Write-Host "Account  : $env:USERNAME"
    Write-Host "Arch     : $env:PROCESSOR_ARCHITECTURE"
    Write-Host "Addresses: $($addrs -join ', ')"
    Write-Host "sshd     : $svcStatus"
    Write-Host "Listen22 : $listen"
    Write-Host "Verdict  : $verdict"

    $body = @()
    $body += "timestamp=$((Get-Date).ToString('s'))"
    $body += "hostname=$env:COMPUTERNAME"
    $body += "account=$env:USERNAME"
    $body += "arch=$env:PROCESSOR_ARCHITECTURE"
    $body += "addresses=$($addrs -join ' ')"
    $body += "sshd=$svcStatus"
    $body += "listen22=$listen"
    $body += "verdict=$verdict"
    $body += "log=$log"
    $body += "notes=" + ($script:notes -join ' | ')

    $written = Write-Result ($body -join "`r`n")
    if ($written) { Write-Host "Result   : $written" }
    else { Write-Host "Result   : (no writable result directory)" }

    Write-Host "Log      : $log"
    Write-Host ""
    Write-Host "--- copy this line back over chat ---" -ForegroundColor Cyan
    Write-Host ("SUMMARY host={0} user={1} arch={2} ip={3} sshd={4} listen22={5} verdict={6}" -f `
        $env:COMPUTERNAME, $env:USERNAME, $env:PROCESSOR_ARCHITECTURE, ($addrs -join '/'), $svcStatus, $listen, $verdict) -ForegroundColor Cyan

    try { Stop-Transcript | Out-Null } catch { }
    if ($verdict -eq 'OK') { exit 0 } else { exit 1 }
}

# ---------------------------------------------------------------- account
$elevated = (New-Object Security.Principal.WindowsPrincipal(
                [Security.Principal.WindowsIdentity]::GetCurrent())
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host "Folder   : $root"
Write-Host "Log      : $log"
Write-Host "Account  : $env:USERNAME"
Write-Host "Elevated : $elevated"
Write-Host ""

if (-not $elevated) {
    Problem "Not running elevated - cannot configure sshd."
    Finish
}

# Informational only; the key file choice does not depend on it.
try {
    $adminSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $m = Get-LocalGroupMember -SID $adminSid -ErrorAction Stop
    $inGroup = [bool]($m | Where-Object { ($_.Name -split '\\')[-1] -ieq $env:USERNAME })
    Write-Host "In Administrators : $inGroup"
} catch {
    Write-Host "In Administrators : (could not determine - not important)"
}
Write-Host ""

# ---------------------------------------------------------------- sshd
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    Problem "sshd service not found. The OpenSSH Server MSI is missing or its install failed."
    Finish
}

Write-Host "[1] Enabling sshd (automatic start)"
Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service  -Name sshd -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- firewall
Write-Host "[2] Firewall: allow inbound TCP 22 (all profiles)"
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
    Note "firewall rule created"
} else {
    Note "firewall rule already present"
}
Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any

# ---------------------------------------------------------------- keys
Write-Host "[3] Installing public keys from keys.txt"

$keysFile = Join-Path $root 'keys.txt'
if (-not (Test-Path -LiteralPath $keysFile)) {
    Problem "keys.txt not found next to configure.ps1 (expected: $keysFile)"
    Finish
}

$keys = @(Get-Content -LiteralPath $keysFile -ErrorAction SilentlyContinue |
          Where-Object { $_ -and $_.Trim() -ne '' -and -not $_.Trim().StartsWith('#') } |
          ForEach-Object { $_.Trim() })

if ($keys.Count -eq 0) {
    Problem "keys.txt contains no usable key lines."
    Finish
}
if (@($keys | Where-Object { ($_ -split '\s+').Count -lt 2 }).Count -gt 0) {
    Problem "keys.txt has malformed line(s); expected: <type> <base64> [comment]"
    Finish
}
Note "keys found: $($keys.Count)"

# Administrator-only design: this file is read for every administrator login.
$target = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
Write-Host "    target: $target"

New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null

$existing = @()
if (Test-Path -LiteralPath $target) {
    $existing = @(Get-Content -LiteralPath $target -ErrorAction SilentlyContinue |
                  Where-Object { $_ -and $_.Trim() -ne '' })
}

$added = 0
foreach ($k in $keys) {
    $body = ($k -split '\s+')[1]
    if (-not ($existing | Where-Object { $_.Contains($body) })) {
        $existing = @($existing) + $k
        $added++
    }
}

try {
    Set-Content -LiteralPath $target -Value $existing -Encoding ascii -ErrorAction Stop
} catch {
    Problem "cannot write $target : $($_.Exception.Message)"
    Finish
}
Note "keys added: $added (total now: $($existing.Count))"

icacls "$target" /inheritance:r | Out-Null
icacls "$target" /grant "*S-1-5-32-544:F" | Out-Null
icacls "$target" /grant "*S-1-5-18:F" | Out-Null
icacls "$target" /setowner "*S-1-5-32-544" 2>$null | Out-Null
Note "ACL: Administrators + SYSTEM only"

# ---------------------------------------------------------------- restart
Write-Host "[4] Restarting sshd"
Restart-Service sshd -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

if ((Get-Service sshd -ErrorAction SilentlyContinue).Status -ne 'Running') {
    Problem "sshd is not Running after restart"
} else {
    Note "sshd Running"
}

$listen = @(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)
if ($listen.Count -eq 0) {
    Problem "nothing is listening on port 22"
} else {
    Note "listening on port 22"
}

Write-Host ""
Write-Host "--- authorized keys now in place ---"
Get-Content -LiteralPath $target

Finish
