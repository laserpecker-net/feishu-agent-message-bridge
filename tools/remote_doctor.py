#!/usr/bin/env python3
"""remote-doctor：从本机通过 SSH 读回 Windows / WSL2 / macOS 的只读事实。

为什么需要这个工具：Windows 侧有一批「⚠需真机」的结论（WSL 版本、发行版是不是 2、
/etc/wsl.conf 有没有 systemd、XDG_RUNTIME_DIR、首启是不是 root、PowerShell 版本、
Windows OpenSSH Server 起没起）在 Mac 上无法静态验证，也看不到对端到底发生了什么。

Tailscale 在这里只负责**网络层**（MagicDNS 名字 + 设备认证 + NAT 穿透）；SSH 认证仍是
OpenSSH，所以本工具只需要系统 `ssh`，不引入 paramiko 之类的新依赖。Windows 原生不提供
Tailscale SSH server，必须装 Windows OpenSSH Server；WSL2/Linux 才可以用 `tailscale up --ssh`。

约束（与 CONTRACTS.md 一致）：

- **只读**：远端只执行查询类命令（Get-Service / cat / systemctl show-environment …），
  不写文件、不改配置、不跑 install.py / *.ps1。工具里没有 --write 这样的开关。
- Windows 走 `powershell -EncodedCommand`（UTF-16LE base64），POSIX 走
  `sh -c "printf %s <b64> | base64 -d | sh"`，规避 cmd.exe 与多层引号转义。
- 输出 JSON（默认）或 Markdown；`--print-commands` 只打印将执行的 ssh 命令，不连接。
- 存在 fail 项时退出码 1；纯 warn/ok 退出码 0；连不上/用法错误退出码 2。
"""
import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path
import time

BANNED = (
    'Start-Service', 'Stop-Service', 'Set-Service', 'New-Item', 'New-ItemProperty',
    'Remove-Item', 'Set-Content', 'Out-File', 'Add-WindowsCapability',
    'install.py', 'setup.sh', 'wsl --install', 'wsl --unregister', 'git pull',
    'apt-get install', 'apt install', 'npm install', 'pip install', 'sudo ',
)

POSIX_SCRIPT = r'''
set -u
prefix=${SENTINEL:-@@}
block() {
  name=$1; shift
  printf '%s%s\n' "$prefix" "$name"
  "$@" 2>&1 || true
  printf '\n'
}
block uname uname -s
block kernel uname -r
block osrelease sh -c 'cat /etc/os-release 2>/dev/null || echo missing'
block uid id -u
block user id -un
block wslconf sh -c 'cat /etc/wsl.conf 2>/dev/null || echo missing'
block systemd sh -c 'export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}; systemctl --user show-environment >/dev/null 2>&1 && echo ok || echo fail'
block xdg sh -c 'printf %s "${XDG_RUNTIME_DIR:-}"'
block p1 sh -c 'ps -p 1 -o comm= 2>/dev/null || echo unknown'
block python3 sh -c 'python3 --version 2>&1 || echo missing'
block git sh -c 'git --version 2>&1 || echo missing'
block wsl_exe sh -c 'command -v wsl.exe >/dev/null 2>&1 && echo present || echo missing'
block tailscale sh -c 'command -v tailscale >/dev/null 2>&1 && tailscale status --json 2>/dev/null | head -c 2000 || echo missing'
'''

WINDOWS_SCRIPT = r'''
$ErrorActionPreference = 'Continue'
# 中文 Windows 控制台默认 936，强制 UTF-8，否则本地按 UTF-8 解码会崩
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

"@@os"
try { $k = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'; $k.ProductName + ' ' + [Environment]::OSVersion.Version.ToString() } catch { 'error: ' + $_.Exception.Message }

"@@ps"
$PSVersionTable.PSEdition + ' ' + $PSVersionTable.PSVersion.ToString()

"@@sshd"
$s = Get-Service sshd -ErrorAction SilentlyContinue
if ($s) { $s.Status.ToString() + ' ' + $s.StartType.ToString() } else { 'missing' }

"@@wslList"
try { ((wsl.exe --list --verbose 2>&1 | Out-String) -replace "`0", '').Trim() } catch { 'missing' }

"@@git"
try { (git --version 2>&1 | Out-String).Trim() } catch { 'missing' }

"@@tailscale"
try { (tailscale status --json 2>&1 | Out-String).Trim() } catch { 'missing' }

"@@wslfacts"
$b64 = '__POSIX_B64__'
try {
  $names = ((wsl.exe --list --quiet 2>&1 | Out-String) -replace "`0", '') -split "`r?`n" | Where-Object { $_.Trim() }
  $distro = ($names | Select-Object -First 1).Trim()
  if ($distro) { (& wsl.exe -d $distro -- bash -c "export SENTINEL='%%'; printf %s $b64 | base64 -d | bash" 2>&1 | Out-String).Trim() } else { 'missing' }
} catch { 'error: ' + $_.Exception.Message }
'''


def posix_command():
    encoded = base64.b64encode(POSIX_SCRIPT.encode('utf-8')).decode('ascii')
    return 'sh -c "printf %%s %s | base64 -d | sh"' % encoded


def windows_command():
    script = WINDOWS_SCRIPT.replace('__POSIX_B64__',
                                    base64.b64encode(POSIX_SCRIPT.encode('utf-8')).decode('ascii'))
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + encoded


def parse_blocks(text, prefix='@@'):
    """把 `@@name`（或指定前缀）的分块输出解析成 dict；值两端空白去掉，块内换行保留。"""
    blocks, current, buffer = {}, None, []
    for line in (text or '').splitlines():
        if line.startswith(prefix) and len(line) > len(prefix) and ' ' not in line:
            if current is not None:
                blocks[current] = '\n'.join(buffer).strip()
            current, buffer = line[len(prefix):], []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        blocks[current] = '\n'.join(buffer).strip()
    return blocks


class RemoteError(RuntimeError):
    pass


class Runner:
    """系统 ssh 的薄封装；测试里替换 run() 即可，不发真实连接。"""

    def __init__(self, host, user=None, port=None, key=None, options=(), timeout=20, command=None):
        self.host = host
        self.user = user
        self.port = port
        self.key = key
        self.options = list(options)
        self.timeout = timeout
        self.command = command or 'ssh'
        self.last_argv = None

    def argv(self, remote_command):
        argv = [self.command, '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=%d' % self.timeout]
        for option in self.options:
            argv += ['-o', option]
        if self.port:
            argv += ['-p', str(self.port)]
        if self.key:
            argv += ['-i', self.key]
        argv += [self.host if not self.user else '%s@%s' % (self.user, self.host), remote_command]
        return argv

    def run(self, remote_command):
        self.last_argv = self.argv(remote_command)
        try:
            result = subprocess.run(self.last_argv, capture_output=True,
                                    timeout=max(self.timeout * 3, 30))
        except FileNotFoundError:
            raise RemoteError('找不到 ssh 可执行文件：%s' % self.command)
        except subprocess.TimeoutExpired:
            raise RemoteError('ssh 超时（%s）' % self.host)
        stdout = _decode(result.stdout)
        stderr = _decode(result.stderr)
        if result.returncode != 0:
            detail = (stderr or stdout or '').strip()
            raise RemoteError('ssh 退出码 %d：%s' % (result.returncode, detail[:400]))
        return stdout


def _decode(raw):
    """ssh 回来的可能是 UTF-8（Linux/macOS/WSL），也可能是 Windows 的 GBK。

    不能假设 UTF-8：中文 Windows 上原生命令输出走 936 代码页，
    strict 解码会直接抛 UnicodeDecodeError 把整个体检搞崩。
    """
    if raw is None:
        return ''
    if isinstance(raw, str):
        return raw
    for encoding in ('utf-8', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _clean(value):
    return (value or '').replace('\x00', '').strip()


def detect_target(runner):
    """先试 POSIX 的 uname；失败再试 Windows 的 PowerShell 探针。"""
    try:
        out = _clean(runner.run('uname -s'))
        if out.startswith('Linux'):
            return 'linux'
        if out.startswith('Darwin'):
            return 'macos'
    except RemoteError:
        pass
    probe = 'powershell -NoProfile -NonInteractive -Command "$PSVersionTable.PSEdition"'
    try:
        if _clean(runner.run(probe)):
            return 'windows'
    except RemoteError:
        pass
    raise RemoteError('无法判断对端系统：uname 与 PowerShell 探针都失败（对端没有 sshd？）')


def collect(runner, target):
    if target == 'windows':
        blocks = parse_blocks(runner.run(windows_command()))
        facts = {'os': blocks.get('os', ''), 'ps': blocks.get('ps', ''),
                 'sshd': blocks.get('sshd', ''), 'wsl_version': blocks.get('wslVersion', ''),
                 'wsl_list': _clean(blocks.get('wslList', '')), 'git': blocks.get('git', ''),
                 'tailscale': blocks.get('tailscale', '')}
        wsl = parse_blocks(blocks.get('wslfacts', ''), prefix='%%')
        facts['wsl'] = {key: wsl.get(key, '') for key in
                        ('uname', 'kernel', 'uid', 'user', 'wslconf', 'systemd', 'xdg',
                         'p1', 'python3', 'git', 'wsl_exe', 'osrelease', 'tailscale')}
        return facts
    blocks = parse_blocks(runner.run(posix_command()))
    return {key: blocks.get(key, '') for key in
            ('uname', 'kernel', 'osrelease', 'uid', 'user', 'wslconf', 'systemd', 'xdg',
             'p1', 'python3', 'git', 'wsl_exe', 'tailscale')}


def _tailscale_summary(text):
    """把 `tailscale status --json` 压成一行：版本 / 后端状态 / 本机 Tailscale IP / 健康项。

    诊断报告要能一眼看出「Tailscale 到底通没通」，而不是让人在一屏 JSON 里找字段。
    """
    raw = _clean(text)
    if not raw or raw == 'missing':
        return ''
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.splitlines()[0][:120]
    if not isinstance(data, dict):
        return raw[:120]
    version = ((data.get('Version') or '').split('-') or [''])[0]
    backend = data.get('BackendState') or '?'
    ips = ((data.get('Self') or {}).get('TailscaleIPs') or [])
    dns = ((data.get('Self') or {}).get('DNSName') or '').rstrip('.')
    health = data.get('Health')
    parts = ['backend=' + str(backend)]
    if version:
        parts.insert(0, 'v' + str(version))
    if ips:
        parts.append('ip=' + str(ips[0]))
    if dns:
        parts.append('dns=' + dns)
    if isinstance(health, list):
        parts.append('health=%d' % len(health))
    return ' '.join(parts)


def _finding(identifier, title, status, detail, hint=''):
    return {'id': identifier, 'title': title, 'status': status,
            'detail': detail, 'hint': hint}


def _wsl_version_numbers(text):
    """从 `wsl --list --verbose` 解析 (name, version)；忽略表头。"""
    rows = []
    for line in _clean(text).splitlines():
        line = line.strip()
        if not line or line.upper().startswith('NAME'):
            continue
        line = line.lstrip('*').strip()
        parts = line.split()
        if len(parts) >= 3 and parts[-1].isdigit():
            rows.append((parts[0], int(parts[-1])))
    return rows


def _wslconf_has_systemd(text):
    lines = [line.strip() for line in _clean(text).splitlines()]
    in_boot = False
    for line in lines:
        if line.startswith('['):
            in_boot = line.replace(' ', '') == '[boot]'
            continue
        if in_boot and line.replace(' ', '').startswith('systemd='):
            return line.replace(' ', '').endswith('=true')
    return False


def verdicts(target, facts):
    findings = []
    if target == 'windows':
        findings.append(_finding('win.os', 'Windows 版本', 'info' if facts.get('os') else 'unknown',
                                 facts.get('os') or '未读到', ''))
        edition = (facts.get('ps') or '').split()
        findings.append(_finding(
            'win.ps', 'PowerShell 版本', 'ok' if edition else 'unknown',
            facts.get('ps') or '未读到',
            '' if not edition or edition[0] == 'Desktop' else
            'PowerShell 7 也要保持脚本兼容 5.1（.ps1 目标读者是默认 Windows）'))
        sshd = (facts.get('sshd') or '').strip()
        if sshd.startswith('Running') and 'Automatic' in sshd:
            findings.append(_finding('win.sshd', 'Windows OpenSSH Server', 'ok', sshd, ''))
        elif sshd == 'missing':
            findings.append(_finding(
                'win.sshd', 'Windows OpenSSH Server', 'fail', '未安装 sshd 服务',
                '管理员 PowerShell：Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0；'
                'Start-Service sshd；Set-Service -Name sshd -StartupType Automatic'))
        else:
            findings.append(_finding(
                'win.sshd', 'Windows OpenSSH Server', 'fail', sshd,
                '不是 Running+Automatic：Start-Service sshd && Set-Service -Name sshd -StartupType Automatic'))
        version = facts.get('wsl_version') or ''
        rows = _wsl_version_numbers(facts.get('wsl_list'))
        kernel = ((facts.get('wsl') or {}).get('kernel') or '').strip()
        # 以发行版列表为准：wsl.exe --version 是 UTF-16 输出，文本编码不稳定，拿它判定会误报
        installed = bool(rows) or ('WSL' in version) or ('microsoft' in kernel.lower())
        detail = (version.splitlines() or [''])[0].strip() or kernel or '未读到'
        findings.append(_finding('win.wsl', 'WSL 已安装', 'ok' if installed else 'fail', detail,
                                 '' if installed else 'wsl --install -d Ubuntu（装完重启，再开一次发行版）'))
        if not rows:
            findings.append(_finding('win.distro', '发行版与 WSL 版本', 'fail',
                                     _clean(facts.get('wsl_list')) or '未读到发行版',
                                     'wsl --install -d Ubuntu；已装则 wsl -l -v 看 VERSION 列'))
        elif all(version == 2 for _, version in rows):
            findings.append(_finding('win.distro', '发行版与 WSL 版本', 'ok',
                                     ', '.join('%s=v%d' % row for row in rows), ''))
        else:
            findings.append(_finding('win.distro', '发行版与 WSL 版本', 'fail',
                                     ', '.join('%s=v%d' % row for row in rows),
                                     'bridge 只在 WSL2 上验证过：wsl --set-version <名称> 2'))
        inner = facts.get('wsl') or {}
        uid = _clean(inner.get('uid'))
        if uid.isdigit():
            findings.append(_finding('win.uid', 'WSL2 默认用户', 'ok' if int(uid) >= 1000 else 'fail',
                                     'uid=%s user=%s' % (uid, _clean(inner.get('user'))),
                                     '' if int(uid) >= 1000 else
                                     '首启是 root：先在该发行版里创建 UNIX 用户（/etc/wsl.conf 与文件属主都依赖它）'))
        else:
            findings.append(_finding('win.uid', 'WSL2 默认用户', 'unknown', '未读到（发行版可能没起来）', ''))
        if _wslconf_has_systemd(inner.get('wslconf')):
            findings.append(_finding('win.wslconf', '/etc/wsl.conf [boot] systemd', 'ok',
                                     _clean(inner.get('wslconf')).splitlines()[0:4].__str__(), ''))
        else:
            findings.append(_finding(
                'win.wslconf', '/etc/wsl.conf [boot] systemd', 'fail',
                _clean(inner.get('wslconf')) or 'missing',
                'systemd 只认发行版 /etc/wsl.conf（不是 .wslconfig）：printf "[boot]\\nsystemd=true\\n" | '
                'wsl.exe -d <发行版> -u root -e tee /etc/wsl.conf，然后 wsl.exe --shutdown 重开'))
        findings.append(_finding('win.systemd', 'systemd --user 可用', 'ok' if _clean(inner.get('systemd')) == 'ok' else 'fail',
                                 _clean(inner.get('systemd')) or '未读到',
                                 '' if _clean(inner.get('systemd')) == 'ok' else
                                 '确认 /etc/wsl.conf 与 XDG_RUNTIME_DIR，见 INSTALL-WINDOWS.md §1'))
        findings.append(_finding('win.xdg', 'XDG_RUNTIME_DIR', 'ok' if _clean(inner.get('xdg')) else 'warn',
                                 _clean(inner.get('xdg')) or '空',
                                 '' if _clean(inner.get('xdg')) else
                                 'WSL2 的 SSH 会话常缺这个变量：export XDG_RUNTIME_DIR=/run/user/$(id -u)'))
        findings.append(_finding('win.p1', 'PID 1 是 systemd', 'ok' if _clean(inner.get('p1')) == 'systemd' else 'warn',
                                 _clean(inner.get('p1')) or '未读到',
                                 '' if _clean(inner.get('p1')) == 'systemd' else 'PID 1 不是 systemd：回 INSTALL-WINDOWS.md §1'))
        findings.append(_finding('win.python3', 'WSL2 里的 python3', 'ok' if _clean(inner.get('python3')).startswith('Python 3')
                                 else 'warn', _clean(inner.get('python3')) or '未读到', ''))
        tailscale_summary = _tailscale_summary(facts.get('tailscale'))
        findings.append(_finding('win.tailscale', 'Tailscale 状态', 'info' if tailscale_summary else 'warn',
                                 tailscale_summary if tailscale_summary else '未安装或无输出',
                                 '' if tailscale_summary else
                                 'Tailscale 只是网络层：Windows 装它，SSH 仍靠 Windows OpenSSH Server'))
        findings.append(_finding('win.git', 'git 可用', 'ok' if _clean(facts.get('git')).startswith('git version') else 'warn',
                                 _clean(facts.get('git')) or '未读到', '引导脚本的 RepoUrl 模式需要 git'))
        return findings

    kernel = _clean(facts.get('kernel'))
    is_wsl = 'microsoft' in kernel.lower()
    findings.append(_finding('posix.os', '系统', 'info',
                             '%s %s' % (_clean(facts.get('uname')), kernel), ''))
    findings.append(_finding('posix.wsl', '是否 WSL2', 'ok' if is_wsl else 'info',
                             'WSL2 内核' if is_wsl else '非 WSL（Linux/macOS 主机）', ''))
    uid = _clean(facts.get('uid'))
    findings.append(_finding('posix.uid', '当前用户', 'ok' if uid.isdigit() and int(uid) >= 1000 else 'warn',
                             'uid=%s user=%s' % (uid, _clean(facts.get('user'))),
                             '' if uid.isdigit() and int(uid) >= 1000 else '首启 root 时先建 UNIX 用户'))
    if is_wsl:
        findings.append(_finding('posix.wslconf', '/etc/wsl.conf [boot] systemd',
                                 'ok' if _wslconf_has_systemd(facts.get('wslconf')) else 'fail',
                                 _clean(facts.get('wslconf')) or 'missing',
                                 '' if _wslconf_has_systemd(facts.get('wslconf')) else
                                 'printf "[boot]\\nsystemd=true\\n" | sudo tee /etc/wsl.conf 后 wsl.exe --shutdown'))
        findings.append(_finding('posix.p1', 'PID 1 是 systemd', 'ok' if _clean(facts.get('p1')) == 'systemd' else 'fail',
                                 _clean(facts.get('p1')) or '未读到',
                                 '' if _clean(facts.get('p1')) == 'systemd' else '回 INSTALL-WINDOWS.md §1'))
    findings.append(_finding('posix.systemd', 'systemd --user 可用',
                             'ok' if _clean(facts.get('systemd')) == 'ok' else 'fail',
                             _clean(facts.get('systemd')) or '未读到',
                             '' if _clean(facts.get('systemd')) == 'ok' else
                             'export XDG_RUNTIME_DIR=/run/user/$(id -u) 后重试；仍失败看 /etc/wsl.conf'))
    findings.append(_finding('posix.xdg', 'XDG_RUNTIME_DIR', 'ok' if _clean(facts.get('xdg')) else 'warn',
                             _clean(facts.get('xdg')) or '空',
                             '' if _clean(facts.get('xdg')) else 'SSH 会话里常缺：export XDG_RUNTIME_DIR=/run/user/$(id -u)'))
    findings.append(_finding('posix.python3', 'python3 版本',
                             'ok' if _clean(facts.get('python3')).startswith('Python 3') else 'warn',
                             _clean(facts.get('python3')) or '未读到', ''))
    posix_tailscale = _tailscale_summary(facts.get('tailscale'))
    findings.append(_finding('posix.tailscale', 'Tailscale 状态',
                             'info' if posix_tailscale else 'warn',
                             posix_tailscale if posix_tailscale else '未安装或无输出',
                             '' if posix_tailscale else
                             'Tailscale 只是网络层；此机可 tailscale up --ssh 提供 SSH server'))
    return findings


def summarize(findings):
    summary = {'ok': 0, 'warn': 0, 'fail': 0, 'info': 0, 'unknown': 0}
    for finding in findings:
        summary[finding['status']] = summary.get(finding['status'], 0) + 1
    return summary


def render_markdown(result):
    lines = ['# remote-doctor 只读体检', '',
             '- 目标：`%s`' % result['host'],
             '- 类型：`%s`' % result['target'],
             '- 时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result['checked_at'])),
             '- 汇总：' + '，'.join('%s=%d' % item for item in sorted(result['summary'].items())),
             '', '| 检查 | 状态 | 事实 | 建议 |', '| --- | --- | --- | --- |']
    for finding in result['findings']:
        detail = (finding['detail'] or '').replace('\n', '<br>').replace('|', '\\|')
        hint = (finding['hint'] or '').replace('|', '\\|')
        lines.append('| %s | %s | %s | %s |' % (finding['title'], finding['status'], detail, hint))
    lines += ['', '> 只读体检：未在对端写文件、改配置或执行 install.py / *.ps1。']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='remote-doctor',
        description='通过 SSH（Tailscale 网络即可）只读体检 Windows / WSL2 / macOS')
    parser.add_argument('--host', required=True, help='对端主机：Tailscale MagicDNS 名或 IP')
    parser.add_argument('--target', choices=['auto', 'windows', 'wsl', 'linux', 'macos'],
                        default='auto', help='对端类型（wsl 等价 linux，仅补充 WSL 专属检查）')
    parser.add_argument('--user', help='SSH 用户')
    parser.add_argument('--port', type=int, help='SSH 端口')
    parser.add_argument('--key', help='私钥路径')
    parser.add_argument('--ssh-option', action='append', default=[], metavar='KEY=VALUE',
                        help='追加 -o 选项，可重复')
    parser.add_argument('--timeout', type=int, default=20, help='连接超时秒数（默认 20）')
    parser.add_argument('--ssh', default='ssh', help='ssh 可执行文件（默认 ssh）')
    parser.add_argument('--json', action='store_true', help='输出 JSON（默认就是 JSON）')
    parser.add_argument('--markdown', metavar='PATH', help='同时写一份 Markdown 报告')
    parser.add_argument('--print-commands', action='store_true', help='只打印将执行的 ssh 命令，不连接')
    parser.add_argument('--strict', action='store_true', help='有 fail 时退出码 1（默认也是 1）')
    args = parser.parse_args(argv)

    runner = Runner(args.host, args.user, args.port, args.key, args.ssh_option,
                    args.timeout, args.ssh)
    try:
        target = args.target if args.target != 'auto' else detect_target(runner)
        if args.print_commands:
            command = windows_command() if target in ('windows', 'wsl') else posix_command()
            print(json.dumps({'host': args.host, 'target': target, 'argv': runner.argv(command)},
                             ensure_ascii=False, indent=2))
            return 0
        if target == 'wsl':
            # WSL 目标：ssh 落在 Windows 上，`sh -c …` 会被 cmd.exe 拒绝（"sh 不是内部或外部命令"）。
            # 走 Windows 采集脚本里那段 wsl.exe 子探测，拿它已经采好的发行版内事实。
            window_facts = collect(runner, 'windows')
            facts = (window_facts.get('wsl') or {})
            if not _clean(facts.get('uname')):
                raise RemoteError('WSL 内部事实为空：发行版没起来，或 Windows 侧 wsl.exe 不可用')
        else:
            facts = collect(runner, target)
    except RemoteError as error:
        print(json.dumps({'ok': False, 'host': args.host, 'error': str(error)},
                         ensure_ascii=False, indent=2))
        return 2

    findings = verdicts(target, facts)
    result = {'host': args.host, 'target': target, 'checked_at': int(time.time()),
              'findings': findings, 'summary': summarize(findings), 'facts': facts}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.markdown:
        # 输出目录（默认 reviews/）在别的机器或新克隆上可能还不存在：裸 open 会 FileNotFoundError。
        parent = Path(args.markdown).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        with open(args.markdown, 'w', encoding='utf-8') as stream:
            stream.write(render_markdown(result))
    return 1 if result['summary'].get('fail') else 0


if __name__ == '__main__':
    sys.exit(main())
