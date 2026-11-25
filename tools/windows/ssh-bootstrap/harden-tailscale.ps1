# =====================================================================
#  harden-tailscale.ps1 -- restrict Windows OpenSSH to the Tailscale range
#
#  Companion to the offline package win-ssh-bootstrap v1.1 (see NOTES.md
#  in this folder). That package opens TCP 22 to "Profile Any" with no
#  remote-address filter and leaves password auth enabled. This script
#  tightens a freshly bootstrapped machine for the feishu-bridge topology:
#
#    1. disable the wide rule  OpenSSH-Server-In-TCP  (if present)
#    2. create/enable  OpenSSH-Server-Tailscale-In-TCP  limited to
#       100.64.0.0/10        (Tailscale IPv4 CGNAT range)
#       fd7a:115c:a1e0::/48  (Tailscale IPv6 range)
#    3. optionally set  PasswordAuthentication no  and
#       KbdInteractiveAuthentication no  in sshd_config (backup kept)
#    4. re-check: sshd Running, TCP 22 listening, effective remote ranges,
#       and the sshd_config lines that matter
#
#  HONESTY NOTE: written without a Windows test machine; reviewed statically
#  only. Verify from the client side afterwards (see NOTES.md section 5 step
#  4). The sshd_config backup it leaves behind is the rollback path.
#
#  Usage (Administrator PowerShell):
#    powershell -ExecutionPolicy Bypass -File .\harden-tailscale.ps1
#    powershell -ExecutionPolicy Bypass -File .\harden-tailscale.ps1 -DisablePassword
#
#  Exit codes: 0 = all checks passed, 1 = a check failed, 2 = prerequisites
# =====================================================================

[CmdletBinding()]
param(
    [string[]]$AllowedRemote = @('100.64.0.0/10', 'fd7a:115c:a1e0::/48'),
    [switch]$DisablePassword,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'

function Fail([string]$m) {
    Write-Host "[X] $m" -ForegroundColor Red
    if (-not $NoPause) { Read-Host 'Press Enter to exit' | Out-Null }
    exit 2
}
function Warn([string]$m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Ok([string]$m)   { Write-Host "[ok] $m" -ForegroundColor Green }

Write-Host '=================================================================='
Write-Host '  Harden Windows OpenSSH to the Tailscale ranges'
Write-Host '=================================================================='
Write-Host ''

# ---------------------------------------------------------------- 0. prerequisites
$elevated = (New-Object Security.Principal.WindowsPrincipal(
                [Security.Principal.WindowsIdentity]::GetCurrent())
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) { Fail 'Not elevated. Run this from an Administrator PowerShell.' }

if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    Fail 'sshd service not found - run install.bat / the MSI first.'
}
Ok 'sshd service present'

if (-not (Test-Path 'C:\Program Files\Tailscale\tailscale.exe')) {
    Warn 'Tailscale does not look installed. Restricting 22 to 100.64.0.0/10 + fd7a:115c:a1e0::/48'
    Warn 'means the machine will be reachable over SSH ONLY through a tailnet.'
    Warn 'That is intended for this topology, but double-check before you lock yourself out.'
}

# ---------------------------------------------------------------- 1. disable wide rule
$wide = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($wide) {
    try {
        Disable-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction Stop
        Ok 'disabled firewall rule OpenSSH-Server-In-TCP (was open to any remote address)'
    }
    catch {
        Warn ("could not disable OpenSSH-Server-In-TCP: " + $_.Exception.Message)
    }
}
else {
    Ok 'no wide rule OpenSSH-Server-In-TCP to disable'
}

# ---------------------------------------------------------------- 2. tailscale-only rule
$name = 'OpenSSH-Server-Tailscale-In-TCP'
$rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($rule) {
    Set-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -RemoteAddress $AllowedRemote | Out-Null
    Set-NetFirewallRule -Name $name -Enabled True -Profile Any -Action Allow | Out-Null
    Ok "updated firewall rule $name"
}
else {
    New-NetFirewallRule -Name $name -DisplayName 'OpenSSH Server (Tailscale only)' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 `
        -RemoteAddress $AllowedRemote -Profile Any | Out-Null
    Ok "created firewall rule $name"
}

# ---------------------------------------------------------------- 3. password auth off
if ($DisablePassword) {
    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $cfg)) { Fail "sshd_config not found: $cfg" }

    $backup = "$cfg.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item -LiteralPath $cfg -Destination $backup -Force
    Ok "backed up sshd_config -> $backup"

    $lines = @(Get-Content -LiteralPath $cfg)
    $seen = @{}
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\s*#?\s*PasswordAuthentication\s+') {
            $out.Add('PasswordAuthentication no'); $seen['PasswordAuthentication'] = $true; continue
        }
        if ($line -match '^\s*#?\s*KbdInteractiveAuthentication\s+') {
            $out.Add('KbdInteractiveAuthentication no'); $seen['KbdInteractiveAuthentication'] = $true; continue
        }
        if ($line -match '^\s*#?\s*PubkeyAuthentication\s+') {
            $out.Add('PubkeyAuthentication yes'); continue
        }
        $out.Add($line)
    }
    foreach ($k in 'PasswordAuthentication', 'KbdInteractiveAuthentication') {
        if (-not $seen.ContainsKey($k)) { $out.Add("$k no") }
    }

    Set-Content -LiteralPath $cfg -Value $out -Encoding ascii
    Ok 'sshd_config: PasswordAuthentication no / KbdInteractiveAuthentication no / PubkeyAuthentication yes'
    Restart-Service sshd
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------- 4. verify
Write-Host ''
Write-Host '------------------------------------------------------------------'
Write-Host 'Verification'
Write-Host '------------------------------------------------------------------'

$problems = 0

$svc = (Get-Service sshd -ErrorAction SilentlyContinue).Status
if ($svc -eq 'Running') { Ok "sshd: $svc" } else { Warn "sshd: $svc"; $problems++ }

$listen = @(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)
if ($listen.Count -gt 0) { Ok 'TCP 22 is listening' } else { Warn 'nothing listening on TCP 22'; $problems++ }

$check = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($check) {
    $remote = @(($check | Get-NetFirewallAddressFilter).RemoteAddress)
    $enabled = $check.Enabled
    Ok "rule $name enabled=$enabled remote=$($remote -join ', ')"
    if ($remote -contains 'Any' -or $remote.Count -eq 0) {
        Warn 'the rule still allows any remote address'; $problems++
    }
}
else {
    Warn "rule $name is missing"; $problems++
}

$cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
if (Test-Path -LiteralPath $cfg) {
    Write-Host ''
    Write-Host 'Effective auth lines in sshd_config (first match wins in sshd):'
    Select-String -LiteralPath $cfg -Pattern '^\s*(#\s*)?(PubkeyAuthentication|PasswordAuthentication|KbdInteractiveAuthentication)\b' |
        ForEach-Object { Write-Host ("    {0,4}: {1}" -f $_.LineNumber, $_.Line.Trim()) }
}

Write-Host ''
Write-Host 'Client-side check from the Mac (must succeed with public key only):'
Write-Host '  ssh -o BatchMode=yes -o PreferredAuthentications=publickey <user>@<magicDNS> "echo SSH_OK"'
Write-Host ''

if ($problems -gt 0) {
    Write-Host "Result: FAIL ($problems problem(s))" -ForegroundColor Red
    if (-not $NoPause) { Read-Host 'Press Enter to exit' | Out-Null }
    exit 1
}

Write-Host 'Result: OK' -ForegroundColor Green
if (-not $NoPause) { Read-Host 'Press Enter to exit' | Out-Null }
exit 0
