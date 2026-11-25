@echo off
rem 双击即可运行：不需要改执行策略、不需要手敲长命令。
rem 这个批处理只做三件事：要管理员、问网关地址、把 install-gateway-cert.ps1 读进内存执行。
rem 为什么要「读进内存」而不是 -File：公司机器常用组策略把执行策略锁成 Restricted/AllSigned，
rem 那种情况下 -ExecutionPolicy Bypass 也会被策略覆盖，报「禁止运行脚本」；而内存执行不受该策略管。
setlocal EnableExtensions
cd /d "%~dp0"
title 安装内网网关根证书

set "PS1=%~dp0install-gateway-cert.ps1"
if not exist "%PS1%" (
  echo [fail] 找不到 install-gateway-cert.ps1，请把它和本文件放在同一个文件夹。
  echo.
  pause
  exit /b 2
)

rem --- 1) 要管理员：装进机器证书库（所有用户/后台服务都信任）---
net session >nul 2>&1
if errorlevel 1 (
  echo 需要管理员权限：正在弹出授权窗口…
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  exit /b 0
)

rem --- 2) 网关地址：命令行给了就用，没给就问 ---
set "GW=%~1"
if "%GW%"=="" (
  echo.
  echo 请输入内网网关地址（IP 或域名，例如 10.0.0.1）：
  set /p "GW=> "
)
if "%GW%"=="" (
  echo [fail] 没填网关地址，什么都没做。
  echo.
  pause
  exit /b 2
)

echo.
echo === 安装证书：%GW% ===
rem --- 3) 读进内存执行（绕过执行策略）---
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $t = Get-Content -Raw -LiteralPath '%PS1%'; & ([scriptblock]::Create($t)) -GatewayHost '%GW%'"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo === 完成。请完全退出 DSH 桌面端再重新打开；仍报证书错误就注销 Windows 再登录。===
) else (
  echo === 没有全部通过（退出码 %RC%）。把上面整段输出发回给宝哥。===
)
echo.
pause
exit /b %RC%
