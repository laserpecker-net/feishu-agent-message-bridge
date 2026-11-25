"""Host platform policy: macOS (launchd) and Linux/WSL2 (systemd) are independent paths.

Only the *platform* differences live here; the bridge itself stays POSIX/Python.
Native Windows is not a supported host: `require_supported()` fails with a
pointer to the WSL2 path, and Windows hosts are expected to run this code
inside WSL2 Ubuntu.
"""
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

LABEL = 'io.feishu-agent.bridge'

#: as_bot 的长连接收消息器（sidecar）单元名；一台机器一种身份，user 模式不该存在。
IDENTITY_SIDECAR_LABEL = 'io.feishu-agent.identity-sidecar'
MAC_HOME = 'Library/Application Support/FeishuPersonalAgent'
LINUX_HOME = '.feishu-personal-agent'


def _detect():
    if sys.platform == 'darwin':
        return 'darwin'
    if sys.platform.startswith('linux'):
        return 'linux'
    if sys.platform == 'win32' or sys.platform in ('cygwin', 'msys'):
        return 'windows'
    return sys.platform


SYSTEM = _detect()


def is_macos():
    return SYSTEM == 'darwin'


def is_linux():
    return SYSTEM == 'linux'


def require_supported():
    """Fail fast on hosts the installer/CLI cannot manage as a service."""
    if SYSTEM == 'windows':
        raise SystemExit('原生 Windows 不受支持；请在 WSL2 (Ubuntu) 中安装，见 INSTALL-WINDOWS.md')
    if SYSTEM not in ('darwin', 'linux'):
        raise SystemExit('不支持的平台：%s；仅支持 macOS 与 Linux/WSL2' % SYSTEM)


def default_home():
    return Path.home() / (MAC_HOME if is_macos() else LINUX_HOME)


def current_user():
    """The installing user; never fall back to the repository author's account."""
    return os.environ.get('USER') or os.environ.get('LOGNAME') or getpass.getuser() or 'unknown'


def default_path():
    """launchd does not inherit the login shell PATH, so build it from this user's home."""
    if is_macos():
        home = Path.home()
        return ':'.join([str(home / '.local/bin'), str(home / '.npm-global/bin'),
                         '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin',
                         '/usr/sbin', '/sbin'])
    return os.environ.get('PATH') or '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


def launcher_shebang(executable=None):
    """Prefer the interpreter that ran install.py; the macOS system shim is only a fallback."""
    if is_macos():
        candidate = executable or sys.executable
        if candidate and Path(candidate).is_file():
            return '#!' + str(Path(candidate).resolve())
        return '#!/usr/bin/python3'
    return '#!/usr/bin/env python3'


def launcher_link():
    return Path.home() / '.local/bin/feishu-bridge'


def _first_file(candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def default_lark_binary():
    home = Path.home()
    candidates = [str(home / '.npm-global/lib/node_modules/@larksuite/cli/bin/lark-cli'),
                  str(home / '.npm-global/bin/lark-cli'),
                  str(home / '.local/bin/lark-cli'),
                  shutil.which('lark-cli'), shutil.which('lark')]
    return _first_file(candidates) or 'lark-cli'


def default_cursor_binary():
    home = Path.home()
    candidates = [str(home / '.local/bin/cursor-agent'),
                  shutil.which('cursor-agent'), shutil.which('agent')]
    return _first_file(candidates) or 'cursor-agent'


def service_dir():
    if is_macos():
        return Path.home() / 'Library/LaunchAgents'
    return Path.home() / '.config/systemd/user'


def service_path(label=LABEL):
    suffix = '.plist' if is_macos() else '.service'
    return service_dir() / (label + suffix)


def _env():
    # PYTHONDONTWRITEBYTECODE：服务从 releases/<id>/ 里跑，写 __pycache__ 会把"不可变副本"
    # 变成会被就地修改的目录（一次运行后 digest 就对不上 receipt 了）。禁止写字节码，
    # release 才能一直保持安装时的样子（Windows 的 unit 里同理）。
    return {'HOME': str(Path.home()), 'USER': current_user(), 'PATH': default_path(),
            'LANG': 'en_US.UTF-8', 'LARKSUITE_CLI_NO_UPDATE_NOTIFIER': '1',
            'LARKSUITE_CLI_NO_SKILLS_NOTIFIER': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


def _plist_payload(home, launcher, label=LABEL, args=('foreground',), log_prefix='launchd'):
    return {'Label': label, 'ProgramArguments': [str(launcher)] + list(args), 'RunAtLoad': True,
            # launchd 本机实测：plist 写 90/120 都只报 exit timeout = 60，按实际值对齐。
            'KeepAlive': True, 'ThrottleInterval': 15, 'ExitTimeOut': 60, 'ProcessType': 'Background',
            'WorkingDirectory': str(home), 'EnvironmentVariables': _env(),
            'StandardOutPath': str(Path(home) / (log_prefix + '.stdout.log')),
            'StandardErrorPath': str(Path(home) / (log_prefix + '.stderr.log'))}


def _systemd_arg(value):
    """Quote/escape one unit value only when systemd needs it (spaces, quotes, %, backslash)."""
    text = str(value)
    if not any(character in text for character in ' \t"\'\\%'):
        return text
    return '"%s"' % text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')


def systemd_unit(home, launcher, label=LABEL, args=('foreground',), log_prefix='systemd'):
    """A user-level unit: `systemctl --user ...`; restart and logs stay host-local."""
    home, launcher = Path(home), Path(launcher)
    env = _env()
    environment = '\n'.join('Environment=%s' % _systemd_arg('%s=%s' % (key, value))
                             for key, value in env.items())
    command = ' '.join(_systemd_arg(item) for item in [launcher] + list(args))
    return '\n'.join([
        '[Unit]',
        'Description=Feishu Personal Assistant (%s)' % label,
        # Keep relaunching like launchd KeepAlive; no rate-limit lockout after a crash loop.
        'StartLimitIntervalSec=0',
        '',
        '[Service]',
        'Type=simple',
        'ExecStart=%s' % command,
        'Restart=on-failure',
        'RestartSec=15',
        'WorkingDirectory=%s' % _systemd_arg(home),
        environment,
        'StandardOutput=append:%s' % _systemd_arg(home / (log_prefix + '.stdout.log')),
        'StandardError=append:%s' % _systemd_arg(home / (log_prefix + '.stderr.log')),
        '',
        '[Install]',
        'WantedBy=default.target',
        '',
    ])


def service_state(label=LABEL):
    """Return {'loaded': bool, 'pid': int|None} for a managed unit (launchd or systemd).

    只回答托管层怎么看，不猜进程表：launchd 用 `launchctl print`，systemd 用 `systemctl show`。
    安装器用它判断"升级前这个单元是否在跑"，README/CLI 用它做同一件事，避免各自解析输出。
    """
    if is_macos():
        target = 'gui/%s/%s' % (os.getuid(), label)
        try:
            done = subprocess.run(['/bin/launchctl', 'print', target],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return {'loaded': False, 'pid': None, 'error': str(error)}
        if done.returncode:
            return {'loaded': False, 'pid': None}
        pid = None
        for line in done.stdout.splitlines():
            text = line.strip()
            if text.startswith('pid = '):
                try:
                    pid = int(text.split('=', 1)[1])
                except ValueError:
                    pid = None
                break
        return {'loaded': True, 'pid': pid}
    environment = dict(os.environ)
    environment.setdefault('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
    for scope in ('user', 'system'):
        command = ['systemctl'] + (['--user'] if scope == 'user' else [])
        try:
            done = subprocess.run(command + ['show', '--property=LoadState,ActiveState,MainPID', label],
                                  capture_output=True, text=True, timeout=30, env=environment)
        except (OSError, subprocess.SubprocessError) as error:
            return {'loaded': False, 'pid': None, 'error': str(error)}
        properties = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
        if properties.get('LoadState') == 'loaded':
            pid = properties.get('MainPID') or ''
            return {'loaded': True, 'pid': int(pid) if pid.isdigit() and int(pid) > 0 else None,
                    'active': properties.get('ActiveState') == 'active', 'scope': scope}
    return {'loaded': False, 'pid': None}


def reload_service(label, service_path, unload_timeout=30, start_timeout=30):
    """把磁盘上刚写好的 unit 重新加载并启动；返回 {'ok','pid','attempts','error'}。

    为什么必须 bootout + bootstrap，而不是 `kickstart -k`：launchd **不会重读磁盘上的 plist**，
    只 kickstart 是"看起来重启了、跑的却还是旧参数/旧代码"（docs/HISTORY.md §9 的
    agentkit-consumerctl 事故、AGENTS §0 的反面案例）。安装器写新 release 后必须走这条路。
    """
    if is_macos():
        target = 'gui/%s/%s' % (os.getuid(), label)
        if service_state(label).get('loaded'):
            subprocess.run(['/bin/launchctl', 'bootout', target],
                           capture_output=True, text=True, timeout=60)
        deadline = time.monotonic() + unload_timeout
        state = service_state(label)
        while state.get('loaded') and time.monotonic() < deadline:
            time.sleep(0.2)
            state = service_state(label)
        if state.get('loaded'):
            return {'ok': False, 'pid': None, 'attempts': 0,
                    'error': 'bootout 之后 %ds 内 launchd 仍未卸载' % unload_timeout}
        errors = []
        for attempt in range(1, 4):
            done = subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/%s' % os.getuid(),
                                   str(service_path)], capture_output=True, text=True, timeout=60)
            if done.returncode == 0:
                break
            errors.append('%s' % ((done.stderr or done.stdout).strip() or 'exit %d' % done.returncode))
            time.sleep(0.5 * attempt)
        else:
            return {'ok': False, 'pid': None, 'attempts': 3, 'error': '; '.join(errors)}
        deadline = time.monotonic() + start_timeout
        state = service_state(label)
        while not state.get('pid') and time.monotonic() < deadline:
            time.sleep(0.2)
            state = service_state(label)
        return {'ok': bool(state.get('loaded')), 'pid': state.get('pid'), 'attempts': attempt,
                'error': None if state.get('loaded') else 'bootstrap 后 launchd 没有加载单元'}
    environment = dict(os.environ)
    environment.setdefault('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
    scope = service_state(label).get('scope', 'user')
    command = ['systemctl'] + (['--user'] if scope == 'user' else [])
    subprocess.run(command + ['daemon-reload'], capture_output=True, text=True, timeout=60, env=environment)
    done = subprocess.run(command + ['restart', label], capture_output=True, text=True,
                          timeout=60, env=environment)
    state = service_state(label)
    return {'ok': done.returncode == 0 and bool(state.get('loaded')), 'pid': state.get('pid'),
            'attempts': 1, 'error': None if done.returncode == 0
            else ((done.stderr or done.stdout).strip() or 'systemctl restart exit %d' % done.returncode)}


def service_definition(home, launcher, label=LABEL, args=('foreground',), log_prefix=None):
    """Return a launchd plist dict on macOS or a systemd unit string on Linux."""
    if log_prefix is None:
        log_prefix = 'launchd' if is_macos() else 'systemd'
    if is_macos():
        return _plist_payload(home, launcher, label=label, args=args, log_prefix=log_prefix)
    if is_linux():
        return systemd_unit(home, launcher, label=label, args=args, log_prefix=log_prefix)
    require_supported()
    raise AssertionError('unreachable')
