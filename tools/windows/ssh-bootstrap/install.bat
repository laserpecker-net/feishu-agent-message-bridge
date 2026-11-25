@echo off
setlocal EnableExtensions
title Windows SSH Bootstrap (OpenSSH Server)

rem ===================================================================
rem  One-click installer: OpenSSH Server + administrator public-key auth
rem  Fleet edition v1.0
rem
rem  Package layout (all files in the same folder as this .bat):
rem    install.bat                 <- this file (double-click)
rem    configure.ps1               <- service / firewall / authorized keys
rem    keys.txt                    <- public keys, one per line
rem    OpenSSH-Win64-*.msi         <- official Microsoft MSI
rem
rem  Usage:
rem    double-click              interactive (self-elevates via UAC)
rem    install.bat --nopause     unattended (RMM / already-elevated)
rem
rem  Exit codes:  0 = success   1 = failure
rem  Idempotent: safe to re-run.
rem ===================================================================

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [X] Cannot enter script folder: %~dp0
    if /i not "%~1"=="--nopause" pause
    exit /b 1
)
set "HERE=%CD%"

echo ==================================================================
echo   Windows SSH Bootstrap (fleet v1.0)
echo   Folder : %HERE%
echo   Host   : %COMPUTERNAME%
echo ==================================================================
echo.

rem ---------- self-elevate ----------
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo Requesting administrator privileges via UAC...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    popd >nul
    exit /b
)

rem ---------- clear Mark-of-the-Web on files delivered via chat / zip ----------
rem  Files received through Feishu (or any chat app), a browser download, or an unzipped
rem  archive carry the "blocked" attribute, which can stop the MSI from
rem  running. Unblocking is harmless when nothing is blocked.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%HERE%' -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

rem ---------- locate the bundled MSI (optional if sshd already present) ----------
set "MSI="
for %%F in ("%HERE%\OpenSSH-Win64-*.msi") do if exist "%%~fF" set "MSI=%%~fF"

rem ---------- only install when sshd is actually missing ----------
sc query sshd >nul 2>&1
if "%errorlevel%"=="0" goto :have_sshd

rem --- sshd is missing: architecture matters because the MSI is arch-specific ---
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
    echo [X] This machine is ARM64. The bundled MSI is x64 and will not install.
    echo     Provide OpenSSH-ARM64-*.msi in this folder instead.
    goto :fail
)

if not defined MSI (
    echo [X] sshd is not installed, and no OpenSSH-Win64-*.msi was found in:
    echo     %HERE%
    goto :fail
)

echo MSI    : %MSI%
echo.
echo [1/2] Installing OpenSSH Server silently (no Windows Update involved)...
start "" /wait msiexec.exe /i "%MSI%" /quiet /norestart
echo     msiexec returned: %errorlevel%

rem The service is the real gate, not msiexec's exit code.
sc query sshd >nul 2>&1
if not "%errorlevel%"=="0" goto :msi_failed
goto :configure

:have_sshd
echo Status : sshd already installed - MSI step skipped
if defined MSI echo MSI    : %MSI%  ^(not needed^)

:configure
echo.
echo [2/2] Configuring service, firewall and authorized keys...
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%\configure.ps1"
set "CRC=%errorlevel%"
echo.
if not "%CRC%"=="0" (
    echo [X] Configuration step failed with code %CRC%
    goto :fail
)

echo ==================================================================
echo   DONE - OpenSSH Server running, administrator keys installed
echo ==================================================================
goto :end

:msi_failed
echo.
echo [X] sshd service still missing after the MSI install.
echo     Run the MSI manually to see its own error dialog:
echo       msiexec /i "%MSI%"
goto :fail

:fail
echo.
echo FAILED - see messages above (log path printed by configure.ps1).
if /i not "%~1"=="--nopause" pause
popd >nul
exit /b 1

:end
echo.
if /i not "%~1"=="--nopause" pause
popd >nul
exit /b 0
