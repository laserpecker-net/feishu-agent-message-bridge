#!/usr/bin/env bash
# feishu-bridge 远程运维：从任意管理员机（macOS/Linux）对目标宿主做状态、体检、挂起/恢复、日志、升级、拆除。
#
# 两种宿主，两种「进到 bridge 所在 shell」的方式：
#   win  Windows 宿主 → SSH 落在 Windows OpenSSH → wsl.exe -d <发行版> -- bash -s
#   mac  macOS 宿主  → SSH 直接落在 POSIX shell → bash -s
# 命令正文一律走 stdin（`bash -s`），不在命令行里拼引号，避免 cmd.exe / 多层转义把命令弄坏；
# 需要 PowerShell 的动作则把 .ps1 传上去用 -File 跑，同样绕开引号。
#
# 只依赖本机 ssh / scp。默认全是只读动作；会改状态的动作（hold/release/upgrade/uninstall）
# 必须在命令行显式点名。
set -uo pipefail

PROG=$(basename "$0")
TARGET="${BRIDGE_TARGET:-win}"            # win | mac
HOST="${BRIDGE_HOST:-}"
REMOTE_USER="${BRIDGE_USER:-}"
DISTRO="${BRIDGE_DISTRO:-Ubuntu}"         # 仅 win 用
BRIDGE_HOME_OPT="${BRIDGE_HOME:-}"        # 目标机上的 --home（一般不用传）
REPO_DIR="${BRIDGE_REPO:-}"               # 目标机仓库路径（默认按宿主推导）
DRY_RUN=0
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
PKG_DIR='C:\ProgramData\feishu-bridge\pkg'     # Windows 侧持久化发行包解压位置
PKG_ZIP='C:\ProgramData\feishu-bridge\bridge-pack.zip'

usage() {
  cat <<EOF
用法: $PROG [选项] <动作> [参数...]

只读动作:
  status                  feishu-bridge status（JSON：mode/held/heartbeat/release…）
  doctor [--assert]       feishu-bridge doctor；--assert 有 fail 级检查项时退出码 1
  logs [行数]             最近日志（默认 80 行）
  version                 已安装 release vs 正在运行 release
  check                   一键体检：doctor --assert（两端）+ Windows 额外跑 WSL 的 verify.sh
  ps-tests                win: 跑发行包自带的两套 PowerShell 测试（需要先 upgrade 过）
会改状态的动作（需显式点名）:
  hold [原因]             挂起：不再采集/入队/回复（内存排队的条目会被丢弃）
  release                 恢复：把挂起窗口永久排除在回填之外
  upgrade <zip>           win: 上传发行包 → 解压到 $PKG_DIR → install-windows.ps1 -Mode Install
  uninstall               win: install-windows.ps1 -Mode Uninstall（拆托管与保活，不删数据）
  quiet-wsl [参数]        win: 关掉 WSLg + 虚拟机空闲回收（写 guiApplications=false /
                          vmIdleTimeout=-1 到 %USERPROFILE%\.wslconfig）
                          用于「桌面每隔几分钟闪一下」（WSLg 的 msrdc.exe 全屏窗口，随 VM 启动出现）；
                          参数透传给脚本：-Check 只读、-Restore 回滚、-NoRestart 只改配置

选项:
  --target win|mac        目标宿主类型（默认 win，或 BRIDGE_TARGET）
  --host HOST             ssh 目标，Tailscale MagicDNS 名或 IP（或 BRIDGE_HOST）
  --user USER             ssh 用户（或 BRIDGE_USER）
  --distro NAME           WSL 发行版（默认 Ubuntu，或 BRIDGE_DISTRO）
  --repo PATH             目标机 WSL/macOS 上的仓库路径（或 BRIDGE_REPO）
  --home PATH             目标机上的 bridge 状态目录（或 BRIDGE_HOME）
  --dry-run               只打印将执行的远端命令，不连接
  -h, --help              本帮助

示例:
  BRIDGE_HOST=win-box.tailnet.ts.net BRIDGE_USER=alice $PROG status
  $PROG --host mac.tailnet.ts.net --user me doctor --assert
  $PROG --host win-box.tailnet.ts.net --user alice check
  $PROG --host win-box.tailnet.ts.net --user alice upgrade ./dist/windows/bridge.zip
EOF
}

log() { printf '[%s] %s\n' "$PROG" "$*" >&2; }
die() { log "错误：$*"; exit 2; }

ACTION=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --user) REMOTE_USER="${2:-}"; shift 2 ;;
    --distro) DISTRO="${2:-}"; shift 2 ;;
    --repo) REPO_DIR="${2:-}"; shift 2 ;;
    --home) BRIDGE_HOME_OPT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "未知选项 ${1}（见 --help）" ;;
    *) ACTION="$1"; shift; ARGS=("$@"); break ;;
  esac
done
[ -n "$ACTION" ] || { usage; exit 2; }
case "$TARGET" in win|mac) ;; *) die "--target 只能是 win 或 mac" ;; esac
case "$ACTION" in
  status|doctor|logs|version|check|ps-tests|hold|release|upgrade|uninstall|quiet-wsl) ;;
  *) die "未知动作 ${ACTION}（见 --help）" ;;
esac
if [ "$TARGET" = mac ]; then
  case "$ACTION" in check|ps-tests|upgrade|uninstall) die "$ACTION 只支持 Windows 宿主" ;; esac
fi
if [ "$DRY_RUN" -eq 0 ] && [ -z "$HOST" ]; then
  die "缺少 --host（或用环境变量 BRIDGE_HOST 指定 Tailscale 名/IP）"
fi

SSH_DEST="$HOST"
[ -n "$REMOTE_USER" ] && SSH_DEST="${REMOTE_USER}@${HOST}"

# 远端 shell 的进入方式
remote_shell() {
  if [ "$TARGET" = win ]; then printf 'wsl.exe -d %s -- bash -s' "$DISTRO"; else printf 'bash -s'; fi
}

# 把一段脚本喂给远端 shell 执行
run_posix() {
  local shell_cmd; shell_cmd=$(remote_shell)
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run：ssh $SSH_DEST '$shell_cmd'，脚本正文："
    sed 's/^/    | /'
    return 0
  fi
  ssh "${SSH_OPTS[@]}" "$SSH_DEST" "$shell_cmd"
}

# 在远端执行 `feishu-bridge <args...>`（PATH 补 ~/.local/bin，非交互 ssh 看不到用户 PATH）
run_bridge() {
  {
    printf 'export PATH="$HOME/.local/bin:$PATH"\n'
    printf 'feishu-bridge'
    [ -n "$BRIDGE_HOME_OPT" ] && printf ' --home %q' "$BRIDGE_HOME_OPT"
    local arg
    for arg in "$@"; do printf ' %q' "$arg"; done
    printf '\n'
  } | run_posix
}

# Windows 侧跑一段 PowerShell（走 stdin，避免 cmd.exe 引号问题）
run_powershell() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run：ssh $SSH_DEST 'powershell -NoProfile -Command -'，脚本正文："
    sed 's/^/    | /'
    return 0
  fi
  ssh "${SSH_OPTS[@]}" "$SSH_DEST" 'powershell -NoProfile -Command -'
}

# Windows 侧跑一个 .ps1（先 scp 到 ProgramData，再用 -File 执行）
run_ps1() {
  local local_script="$1" remote_name="$2" extra="${3:-}"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run：scp $local_script → $SSH_DEST:'C:/ProgramData/feishu-bridge/$remote_name'，再 powershell -File 执行 $extra"
    return 0
  fi
  ssh "${SSH_OPTS[@]}" "$SSH_DEST" 'powershell -NoProfile -Command -' <<PS >/dev/null
\$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path 'C:\ProgramData\feishu-bridge' | Out-Null
PS
  [ $? -eq 0 ] || die "无法准备 C:\\ProgramData\\feishu-bridge"
  scp -q "${SSH_OPTS[@]}" "$local_script" "$SSH_DEST:C:/ProgramData/feishu-bridge/$remote_name" \
    || die "scp 失败：$local_script"
  ssh "${SSH_OPTS[@]}" "$SSH_DEST" \
    "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\ProgramData\\feishu-bridge\\$remote_name $extra"
  local code=$?
  # 成功后删掉上传的临时脚本：它会一直躺在 ProgramData 里，被别的人（或另一个 DSH 会话）
  # 误读成「有人在反复跑升级脚本」。失败时保留，方便排查。
  if [ $code -eq 0 ]; then
    ssh "${SSH_OPTS[@]}" "$SSH_DEST" "del C:\\ProgramData\\feishu-bridge\\$remote_name" >/dev/null 2>&1 || true
  fi
  return $code
}

case "$ACTION" in
  status)  run_bridge status ;;
  # 注意：`"${ARGS[@]:-}"` 在无参数时会展开成一个空字符串参数，远端 argparse 报
  # "unrecognized arguments:"。用 ${ARGS[@]+...} 只在真正有参数时才展开（set -u 下安全）。
  doctor)  run_bridge doctor ${ARGS[@]+"${ARGS[@]}"} ;;
  logs)
    lines="${ARGS[0]:-80}"
    {
      printf 'export PATH="$HOME/.local/bin:$PATH"\n'
      if [ -n "$BRIDGE_HOME_OPT" ]; then
        printf 'feishu-bridge --home %q logs | tail -n %s\n' "$BRIDGE_HOME_OPT" "$lines"
      else
        printf 'feishu-bridge logs | tail -n %s\n' "$lines"
      fi
    } | run_posix
    ;;
  version)
    {
      printf 'export PATH="$HOME/.local/bin:$PATH"\n'
      printf 'BRIDGE_HOME=%q BRIDGE_REPO=%q python3 - <<'"'"'PY'"'"'\n' \
        "$BRIDGE_HOME_OPT" "$REPO_DIR"
      cat <<'PY'
import json, os, sys
home = os.environ.get('BRIDGE_HOME') or None
if not home:
    if sys.platform == 'darwin':
        home = os.path.expanduser('~/Library/Application Support/FeishuPersonalAgent')
    else:
        home = os.path.expanduser('~/.feishu-personal-agent')
def read(name):
    try:
        return json.load(open(os.path.join(home, name)))
    except (OSError, ValueError):
        return {}
installed, status = read('installation.json'), read('status.json')
print(json.dumps({'home': home,
                  'installed_release': installed.get('release_id'),
                  'running_release': status.get('running_release'),
                  'update_pending': bool(installed.get('release_id') and status.get('running_release')
                                         and installed['release_id'] != status['running_release']),
                  'installed_at': installed.get('installed_at'),
                  'mode': status.get('mode'),
                  'heartbeat_age_seconds': status.get('heartbeat_at') and
                      round(__import__('time').time() - status['heartbeat_at'], 1)},
                 ensure_ascii=False, indent=2))
PY
      printf 'PY\n'
    } | run_posix
    ;;
  check)
    # 刚 restart 完 status.json 可能还是上一个进程的：先给一次重试，避免把「正在启动」报成故障。
    run_bridge doctor --assert
    code=$?
    if [ "$code" -ne 0 ]; then
      log '第一次断言未通过，5 秒后重试一次（服务刚重启时 status.json 可能还没刷新）'
      sleep 5
      run_bridge doctor --assert
      code=$?
    fi
    verify_code=0
    if [ "$TARGET" = win ]; then
      {
        # 远端自己展开 $HOME：在 Mac 侧展开会得到 /Users/<me>/... 这种远端不存在的路径
        if [ -n "$REPO_DIR" ]; then printf 'repo=%q\n' "$REPO_DIR"; else printf 'repo="$HOME/feishu-personal-agent"\n'; fi
        cat <<'SH'
cd "$repo" 2>/dev/null || { echo "VERIFY=skip（找不到仓库 ${repo}）"; exit 0; }
[ -f dist/wsl2/wsl/verify.sh ] || { echo "VERIFY=skip（无 dist/wsl2/wsl/verify.sh）"; exit 0; }
# 输出只留末尾 25 行，但**退出码必须是 verify.sh 的**：`verify.sh | tail` 的退出码是 tail 的 0，
# 会把失败吞成绿灯（2026-11-05 真机：verify 报 RESULT=failed，check 仍退出 0）。
out="$(bash dist/wsl2/wsl/verify.sh 2>&1)"; vrc=$?
printf '%s\n' "$out" | tail -n 25
exit "$vrc"
SH
      } | run_posix
      verify_code=$?
    fi
    [ "$code" -ne 0 ] && exit $code
    exit $verify_code
    ;;
  ps-tests)
    # 每套单独一个 PowerShell 进程：测试脚本自己会 exit，串在同一个 -Command 会话里会把输出吞掉。
    if [ "$DRY_RUN" -eq 1 ]; then
      log "dry-run：在 ${SSH_DEST} 上逐个跑发行包自带的两套 PowerShell 测试（-File 模式）"
      exit 0
    fi
    pkg_sub=$(ssh "${SSH_OPTS[@]}" "$SSH_DEST" 'powershell -NoProfile -Command -' <<PS
(Get-ChildItem '${PKG_DIR}' -Directory -ErrorAction SilentlyContinue | Select-Object -First 1).Name
PS
    )
    pkg_sub=$(printf '%s' "$pkg_sub" | tr -d '\r\n')
    if [ -z "$pkg_sub" ]; then
      log "发行包目录不存在（先跑 ${PROG} upgrade <zip>）"
      exit 3
    fi
    tests_dir="C:\\ProgramData\\feishu-bridge\\pkg\\${pkg_sub}\\repo\\dist\\wsl2\\tests"
    fail=0
    for name in WindowsDistribution.Tests.ps1 InstallWindows.Tests.ps1; do
      ssh "${SSH_OPTS[@]}" "$SSH_DEST" \
        "powershell -NoProfile -ExecutionPolicy Bypass -File ${tests_dir}\\${name}" > /tmp/bridge-remote-${name}.log 2>&1
      code=$?
      ok=$(LC_ALL=C grep -ac 'ok:' "/tmp/bridge-remote-${name}.log" 2>/dev/null || echo 0)
      if [ "$code" -eq 0 ]; then
        log "PASSED ${name}（${ok} 项 ok）"
      else
        log "FAILED ${name}（exit=${code}，日志 /tmp/bridge-remote-${name}.log）"
        fail=1
      fi
    done
    exit $fail
    ;;
  hold)
    reason="${ARGS[0]:-远程挂起}"
    run_bridge hold --by remote -r "$reason"
    ;;
  release) run_bridge release --by remote ;;
  quiet-wsl)
    [ "$TARGET" = win ] || die "quiet-wsl 只对 Windows 目标有意义（当前 --target $TARGET）"
    _ps1="$(cd "$(dirname "$0")/.." && pwd)/dist/wsl2/windows/quiet-wsl-flash.ps1"
    [ -f "$_ps1" ] || die "找不到 $_ps1"
    run_ps1 "$_ps1" 'quiet-wsl-flash.ps1' "${ARGS[*]:-}"
    ;;
  uninstall)
    {
      printf '\$ErrorActionPreference = %s\n' "'Stop'"
      cat <<PS
\$repo = (Get-ChildItem '$PKG_DIR' -Directory -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not \$repo) { Write-Output 'PKG=missing（先跑本脚本 upgrade）'; exit 3 }
\$installer = Join-Path \$repo.FullName 'repo\dist\wsl2\install-windows.ps1'
if (-not (Test-Path \$installer)) { Write-Output ('INSTALLER=missing ' + \$installer); exit 4 }
& \$installer -Mode Uninstall
exit \$LASTEXITCODE
PS
    } | run_powershell
    ;;
  upgrade)
    zip_path="${ARGS[0]:-}"
    [ -n "$zip_path" ] || die "upgrade 需要发行包路径：$PROG upgrade <zip>"
    [ -f "$zip_path" ] || die "找不到文件 $zip_path"
    if [ "$DRY_RUN" -eq 1 ]; then
      log "dry-run：scp $zip_path → $SSH_DEST:'$PKG_ZIP' → 解压到 '$PKG_DIR' → install-windows.ps1 -Mode Install"
      exit 0
    fi
    log "上传 ${zip_path}（$(du -h "$zip_path" | cut -f1)）"
    ssh "${SSH_OPTS[@]}" "$SSH_DEST" 'powershell -NoProfile -Command -' <<PS >/dev/null
New-Item -ItemType Directory -Force -Path (Split-Path '$PKG_ZIP') | Out-Null
PS
    scp -q "${SSH_OPTS[@]}" "$zip_path" "$SSH_DEST:C:/ProgramData/feishu-bridge/bridge-pack.zip" \
      || die "scp 失败"
    tmp_ps=$(mktemp /tmp/bridge-upgrade-XXXXXX.ps1)
    # 必须带 UTF-8 BOM：Windows PowerShell 5.1 对**无 BOM** 的文件按 ANSI 解码，中文注释会被
    # 解成乱码，且跨行字节错位会把下一行代码并进注释里（2026-11-04 真机：`$tar = ...` 整行被
    # 吞掉，脚本报 "Test-Path 参数为 null"，升级静默什么都没装）。仓库里的 .ps1 也都带 BOM。
    printf '\xef\xbb\xbf' > "$tmp_ps"
    cat >>"$tmp_ps" <<PS
\$ErrorActionPreference = 'Stop'
\$zip = '$PKG_ZIP'
\$dest = '$PKG_DIR'
if (Test-Path \$dest) { Remove-Item -Recurse -Force \$dest }
New-Item -ItemType Directory -Force -Path \$dest | Out-Null
# 解压不用 Expand-Archive：它的 -Force 是"合并覆盖"，而且模块内部清理已展开项时会抛
# "找不到路径 ...prereq.cmd" 的噪声，真正的失败被淹掉（2026-11-04 真机：升级脚本报成功、
# 实际什么都没装）。也不用 .NET ZipFile：PS 5.1 上 [System.IO.Compression.ZipFile]
# 可能加载不到类型。Windows 10 1803+ 自带 bsdtar，直接用它，失败即失败。
\$tar = Join-Path \$env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path \$tar)) { Remove-Item -Recurse -Force \$dest; Write-Output 'TAR=missing'; exit 5 }
& \$tar -xf \$zip -C \$dest
if (\$LASTEXITCODE -ne 0) { Remove-Item -Recurse -Force \$dest -ErrorAction SilentlyContinue; Write-Output ('TAR=failed exit=' + \$LASTEXITCODE); exit 5 }
\$repo = (Get-ChildItem \$dest -Directory | Select-Object -First 1).FullName
\$installer = Join-Path \$repo 'repo\dist\wsl2\install-windows.ps1'
if (-not (Test-Path \$installer)) {
    # 残包不能装：先删掉，免得下次 Remove-Item 半途失败又留下说不清的中间态。
    Remove-Item -Recurse -Force \$dest -ErrorAction SilentlyContinue
    Write-Output ('INSTALLER=missing ' + \$installer)
    exit 4
}
Write-Output ('PACKAGE=' + \$repo)
& \$installer -SourcePath (Join-Path \$repo 'repo')
exit \$LASTEXITCODE
PS
    run_ps1 "$tmp_ps" 'bridge-upgrade.ps1'
    code=$?
    rm -f "$tmp_ps"
    exit $code
    ;;
esac
