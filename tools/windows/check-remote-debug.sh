#!/usr/bin/env bash
# 从 Mac 侧检查「同事的 Windows」是否连得上，并跑一遍仓库自带的只读体检。
#
# 前提：对端已经跑过 tools/windows/setup-remote-debug.ps1（装了 Tailscale + Windows OpenSSH Server
# 并写入你的公钥）。本脚本只做只读检查，不会在对端写文件、不跑 install.py。
#
# 用法：
#   tools/windows/check-remote-debug.sh --host <MagicDNS名或IP> [--user <Windows用户名>] [--port 22]
#                                      [--target windows|wsl|linux|macos|auto] [--ask-password]
#
# 退出码：0 全部通过；1 体检有 fail 项；2 连不上对端。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST=""
SSH_USER=""
PORT=22
TARGET="windows"
KEY=""
ASK_PASSWORD=0

usage() {
    sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:-}"; shift 2 ;;
        --user) SSH_USER="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --target) TARGET="${2:-}"; shift 2 ;;
        --key) KEY="${2:-}"; shift 2 ;;
        --ask-password) ASK_PASSWORD=1; shift ;;
        -h|--help) usage ;;
        *) echo "未知参数：$1" >&2; usage ;;
    esac
done

if [ -z "$HOST" ]; then
    echo "缺少 --host（对端报告里的 MagicDNS 名，例如 desktop-ab12-win.tailxxxx.ts.net）" >&2
    echo "先跑 --help 看用法" >&2
    exit 2
fi

step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }
bad()  { printf '  [fail] %s\n' "$1"; }

step '本机 Tailscale 状态'
TS_CLI=""
if command -v tailscale >/dev/null 2>&1; then
    TS_CLI="$(command -v tailscale)"
elif [ -x '/Applications/Tailscale.app/Contents/MacOS/Tailscale' ]; then
    TS_CLI='/Applications/Tailscale.app/Contents/MacOS/Tailscale'
fi

if [ -z "$TS_CLI" ]; then
    warn '本机没装 Tailscale（也找不到 /Applications/Tailscale.app）：brew install --cask tailscale-app'
    warn '如果你们在同一局域网/同一 VPN，可以忽略这一条，直接看下面的 SSH 检查。'
else
    ok "Tailscale CLI: $TS_CLI"
    if "$TS_CLI" status >/dev/null 2>&1; then
        "$TS_CLI" status 2>&1 | sed -n '1,15p' | sed 's/^/    /'
    else
        warn 'tailscale status 不可用：Tailscale 没登录或没启动'
    fi
fi

step '本机公钥（必须和对端 authorized_keys 里那把一致）'
if [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
    ok "默认公钥：$(cat "$HOME/.ssh/id_ed25519.pub")"
else
    warn '没有 ~/.ssh/id_ed25519.pub：脚本默认授权的是这把，对端可能因此连不上'
fi
FINGERPRINT_SRC="$HOME/.ssh/id_ed25519.pub"
[ -n "$KEY" ] && FINGERPRINT_SRC="$KEY.pub"
if [ -f "$FINGERPRINT_SRC" ] && command -v ssh-keygen >/dev/null 2>&1; then
    printf '    指纹：%s\n' "$(ssh-keygen -lf "$FINGERPRINT_SRC" 2>/dev/null)"
fi

step 'SSH 连通性'
SSH_OPTS="-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15"
if [ "$ASK_PASSWORD" -eq 0 ]; then
    SSH_OPTS="$SSH_OPTS -o BatchMode=yes"
fi
TARGET_SPEC="$HOST"
if [ -n "$SSH_USER" ]; then
    TARGET_SPEC="$SSH_USER@$HOST"
fi
KEY_OPT=""
if [ -n "$KEY" ]; then
    KEY_OPT="-i $KEY -o IdentitiesOnly=yes"
fi

# shellcheck disable=SC2086
if ssh $SSH_OPTS $KEY_OPT -p "$PORT" "$TARGET_SPEC" 'echo WINDOWS_SSH_OK' 2>/tmp/remote-debug-ssh.err; then
    ok "ssh $TARGET_SPEC 通"
else
    bad "ssh $TARGET_SPEC 不通"
    sed -n '1,6p' /tmp/remote-debug-ssh.err | sed 's/^/    /'
    cat <<'HINT'
    常见原因（按顺序排查）：
      1) 对端 OpenSSH Server 没起：在对端 PowerShell 跑 Get-Service sshd（应为 Running）
      2) 用户名不对：用对端 whoami 里反斜杠后面那段；有密码的话加 --ask-password 先确认能登
      3) 公钥没生效：管理员账号看 C:\ProgramData\ssh\administrators_authorized_keys（不是 ~/.ssh）
      4) Windows 防火墙只信 Tailscale 网段：确认双方在同一 tailnet（tailscale status 里看得到对端）
      5) Tailscale 需要设备审批：管理后台 Devices 里把这台批准
HINT
    exit 2
fi

step '只读体检（tools/remote_doctor.py，不写对端）'
MD="$ROOT/reviews/remote-doctor-$(date +%F).md"
DOCTOR_ARGS=(--host "$HOST" --target "$TARGET" --port "$PORT")
if [ -n "$SSH_USER" ]; then DOCTOR_ARGS+=(--user "$SSH_USER"); fi
if [ -n "$KEY" ]; then DOCTOR_ARGS+=(--key "$KEY"); fi
mkdir -p "$ROOT/reviews"
python3 "$ROOT/tools/remote_doctor.py" "${DOCTOR_ARGS[@]}" --markdown "$MD"
CODE=$?
if [ -f "$MD" ]; then
    ok "Markdown 报告：$MD"
fi

step '接着怎么调 bridge（对端 bridge 在 WSL2 里时）'
cat <<EOF
    # 看发行版
    ssh -p $PORT $TARGET_SPEC 'wsl.exe --list --verbose'
    # 进 WSL2 看 bridge 状态（只读；不要在对端跑 install.py 或 start/stop）
    ssh -p $PORT $TARGET_SPEC 'wsl.exe -d Ubuntu -- bash -lc "feishu-bridge status"'
    ssh -p $PORT $TARGET_SPEC 'wsl.exe -d Ubuntu -- bash -lc "feishu-bridge logs"'
EOF

exit $CODE
