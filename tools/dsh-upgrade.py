#!/usr/bin/env python3
"""安全升级 DSH：把「就地覆盖正在跑的那套工具」这件事从人手里拿走。

背景（2026-11-18 事故，见 ``AGENTS.md`` §8 与 ``docs/HISTORY.md``）：在 ``dsh web`` 正跑着
（会话就跑在它里面）的时候手敲 ``npm i -g @deepseek-ai/dsh@x.y.z``，``node_modules`` 被
就地换掉，宿主与磁盘不一致，**所有会话失效**。事后定的规矩是「停 → 装 → 起 → 验证」，
这个脚本就是那条规矩的可执行版本。

用法::

    python3 tools/dsh-upgrade.py --check                 # 只做体检，零写入
    python3 tools/dsh-upgrade.py --to 0.2.2-rc.4 --dry-run   # 打印完整计划，零写入
    python3 tools/dsh-upgrade.py --to 0.2.2-rc.4          # 真升级（默认要输入 yes）
    python3 tools/dsh-upgrade.py --rollback               # 按状态文件回到上一版本
    python3 tools/dsh-upgrade.py --to latest --yes        # 非交互

退出码：**0 成功 / 2 拒绝（体检没过、参数不对）/ 3 失败但已回滚 / 4 回滚也失败**。

设计上的四条硬边界：

1. **宿主在跑就一个字都不动**：这是事故的直接原因，必须挡在第一道。
2. **受保护资产升级前后逐项比对**：凭证、`AGENTS.md`、配置、会话、证书、启动器——
   改了或删了就是升级失败，自动回滚（新增只警告，因为工作台自己会写新会话）。
3. **只写一个目录**：除 npm 全局安装外，只写 ``~/Library/Application Support/dsh-upgrade/``
   （备份、状态、审计）。``--check``/``--dry-run`` 连这个目录都不写。
4. **失败必须留证据**：每次动作（含被拒绝）追加一行 JSON 审计，回滚按状态文件执行——
   不靠现场判断，也不靠人记。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

#: 退出码。3/4 是"升级失败"，区别在于救没救回来——上层脚本据此决定要不要叫人。
EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_ROLLED_BACK = 3
EXIT_ROLLBACK_FAILED = 4

PACKAGE = '@deepseek-ai/dsh'
#: 少于这个可用空间就不升级：npm 解包 + 备份 + 回滚都要地方。
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
#: 会话文件在这个秒数内被写过，就算"有人在用"。
DEFAULT_SESSION_WINDOW = 300.0
#: 小于等于这个大小的文件算 sha256；更大的只比大小与时间（不值得为它读一整遍）。
HASH_LIMIT = 2 * 1024 * 1024
#: GUI 可能监听的端口（旧版 3080、新版 8765）。
GUI_PORTS = (8765, 3080)
#: npm 安装脚本白名单（`--allow-scripts` 时原样传给 npm）。
ALLOW_SCRIPTS = '@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs'

#: `~/.dsh` 里不参与比对的子树：体积大、纯缓存，且升级本来就会动它们。
DSH_HOME_EXCLUDED_TOP = ('cache',)
DSH_HOME_EXCLUDED_PREFIX = ('llm-',)


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(command, timeout=180, env=None):
    """跑一条外部命令，返回 (退出码, stdout, stderr)。**不抛异常**，调用方看码说话。"""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                   env=env, check=False)
    except FileNotFoundError as error:
        return 127, '', '找不到命令：%s' % error
    except subprocess.TimeoutExpired:
        return 124, '', '命令超时（%s 秒）：%s' % (timeout, ' '.join(map(str, command)))
    return completed.returncode, completed.stdout, completed.stderr


class Environment:
    """所有路径与外部命令的唯一来源。测试通过环境变量把它整体指向临时目录。"""

    def __init__(self):
        home = Path(os.environ.get('HOME') or str(Path.home()))
        self.home = home
        self.dsh_home = Path(os.environ.get('DSH_HOME') or str(home / '.dsh'))
        default_support = home / 'Library' / 'Application Support'
        self.app_support = Path(os.environ.get('DSH_UPGRADE_APP_SUPPORT') or str(default_support))
        self.state_dir = Path(os.environ.get('DSH_UPGRADE_STATE_DIR')
                              or str(self.app_support / 'dsh-upgrade'))
        self.npm = os.environ.get('DSH_UPGRADE_NPM', 'npm')
        self.dsh = os.environ.get('DSH_UPGRADE_DSH', 'dsh')
        self.node = os.environ.get('DSH_UPGRADE_NODE', 'node')
        self.platform = os.environ.get('DSH_UPGRADE_PLATFORM', sys.platform)
        self.install_dir = os.environ.get('DSH_UPGRADE_INSTALL_DIR')  # 测试注入；否则问 npm root -g
        self.repo_dir = Path(os.environ.get('DSH_UPGRADE_REPO_DIR') or os.getcwd())
        self.session_window = float(os.environ.get('DSH_UPGRADE_SESSION_WINDOW',
                                                   DEFAULT_SESSION_WINDOW))
        self.free_bytes_override = os.environ.get('DSH_UPGRADE_FREE_BYTES')
        #: '1' = 假装宿主在跑（不该真去 ps）；'0' = 假装没跑（测试用，避免扫到真实进程）
        self.fake_host = os.environ.get('DSH_UPGRADE_FAKE_HOST')

    # -- 路径 ---------------------------------------------------------------
    @property
    def backups_dir(self):
        return self.state_dir / 'backups'

    @property
    def state_file(self):
        return self.state_dir / 'state.json'

    @property
    def audit_file(self):
        return self.state_dir / 'audit.jsonl'

    def free_bytes(self):
        if self.free_bytes_override is not None:
            return int(self.free_bytes_override)
        probe = self.state_dir if self.state_dir.exists() else self.state_dir.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(str(probe)).free

    def package_dir(self):
        if self.install_dir:
            return Path(self.install_dir)
        code, out, _ = run([self.npm, 'root', '-g'], timeout=60)
        if code != 0 or not out.strip():
            return None
        return Path(out.strip()) / PACKAGE


# ---------------------------------------------------------------- 体检 ----

def _process_table():
    """(pid, 命令行) 列表；拿不到就返回空（调用方按"没在跑"处理，不猜）。"""
    code, out, _ = run(['ps', '-ax', '-o', 'pid=,args='], timeout=30)
    if code != 0:
        return []
    table = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(' ')
        try:
            table.append((int(pid), args.strip()))
        except ValueError:
            continue
    return table


def _listening_ports(port):
    """谁在听这个端口：返回 ['node'] 这类命令名列表；lsof 不可用时返回空。"""
    code, out, _ = run(['lsof', '-nP', '-iTCP:%d' % port, '-sTCP:LISTEN', '-Fcp'], timeout=30)
    if code != 0 or not out:
        return []
    commands = []
    for line in out.splitlines():
        if line.startswith('c'):
            commands.append(line[1:].strip())
    return commands


def _port_open(port, timeout=0.3):
    """端口能不能连上。lsof 在部分机器上要权限（拿不到结果），socket 是最后的兜底。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            return probe.connect_ex(('127.0.0.1', port)) == 0
    except OSError:
        return False


def _looks_like_host(args):
    """这条命令行像不像"正在托管会话的 dsh web"。

    三种起法都要认（2026-11-18 那次就是漏了第三种，差点把第一道门变成摆设）：

    * ``npm exec @deepseek-ai/dsh web``        —— npm exec 直起
    * ``node …/@deepseek-ai/dsh/lib/bin.js web`` —— 直接 node 起 bin.js
    * ``node …/install-packs/<包>/launch-dsh.mjs --bin …/dsh/lib/bin.js web`` —— 启动器形态（当前机器就是这种）
    """
    if 'dsh-upgrade' in args:
        return False                     # 本脚本自己
    tokens = args.split()
    if 'web' not in tokens:
        return False
    if any(token.startswith('@deepseek-ai/dsh') for token in tokens):
        return True
    if any(token.endswith('bin.js') for token in tokens):
        return True
    #: 直接敲的 ``dsh web`` / ``/path/to/dsh web``
    if any(token == 'dsh' or token.endswith('/dsh') or token.endswith('/dsh.cmd')
           for token in tokens):
        return True
    if 'launch-dsh' in args:
        return True
    return '/dsh/' in args or '/dsh ' in args


def host_running(env):
    """宿主是否在跑。返回 (bool, 说明列表)。**这是第一道门，宁严勿松。**"""
    if env.fake_host == '1':
        return True, ['DSH_UPGRADE_FAKE_HOST=1（测试注入：假装宿主在跑）']
    if env.fake_host == '0':
        return False, []
    reasons = []
    for pid, args in _process_table():
        if pid == os.getpid():
            continue
        if _looks_like_host(args):
            reasons.append('pid %d：%s' % (pid, args[:120]))
    for port in GUI_PORTS:
        commands = _listening_ports(port)
        names = ','.join(commands)
        if (commands and any('node' in name for name in commands)) or _port_open(port):
            reasons.append('127.0.0.1:%d 有人在听%s' % (port, '（%s）' % names if names else ''))
    return bool(reasons), reasons


def recent_session_writes(env, now=None):
    """会话目录里最近被写过的文件（含子目录自身的 mtime——写文件会碰目录）。"""
    now = time.time() if now is None else now
    root = env.dsh_home / 'sessions'
    if not root.is_dir():
        return []
    hits = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in list(dirnames) + list(filenames):
            candidate = os.path.join(dirpath, name)
            try:
                mtime = os.stat(candidate).st_mtime
            except OSError:
                continue
            if now - mtime <= env.session_window:
                hits.append((candidate, mtime))
    hits.sort(key=lambda item: item[1], reverse=True)
    return hits


def available_versions(env):
    """npm 上的版本清单。返回 (versions, error)。"""
    code, out, err = run([env.npm, 'view', PACKAGE, 'versions', '--json'], timeout=180)
    if code != 0:
        return [], '查询版本失败：%s' % (err.strip() or out.strip() or 'rc=%d' % code)
    try:
        versions = json.loads(out)
    except ValueError:
        return [], '版本清单不是 JSON：%s' % out.strip()[:200]
    if isinstance(versions, str):
        versions = [versions]
    if not isinstance(versions, list):
        return [], '版本清单形状不对：%r' % (versions,)[:200]
    return [str(item) for item in versions if isinstance(item, (str, int, float))], None


def resolve_target(env, wanted):
    """把 ``latest`` 之类解析成具体版本号。返回 (version, error, available)。"""
    versions, error = available_versions(env)
    if error:
        return None, error, []
    if wanted in (None, ''):
        return None, '没给版本：用 --to <版本>（或 --to latest）', versions
    if wanted == 'latest':
        if not versions:
            return None, 'npm 上没有可用版本', versions
        return versions[-1], None, versions
    if wanted not in versions:
        return None, '版本 %s 不存在' % wanted, versions
    return wanted, None, versions


def preflight(env, target):
    """升级前的全部前提。返回问题列表；空列表才放行。"""
    problems = []
    running, reasons = host_running(env)
    if running:
        problems.append(('宿主在跑', 'dsh web / GUI 还活着：%s\n  → 先停 GUI（连同所有会话）再升级'
                         % '；'.join(reasons)))
    hits = recent_session_writes(env)
    if hits:
        shown = '\n  → '.join('%s（%d 秒前）' % (path, int(time.time() - mtime))
                               for path, mtime in hits[:5])
        problems.append(('有活跃会话', '最近 %d 秒内有会话在写：\n  → %s'
                         % (int(env.session_window), shown)))
    if env.platform != 'darwin':
        problems.append(('平台不支持', '这个脚本只处理 macOS（当前 %s）' % env.platform))
    package_dir = env.package_dir()
    if package_dir is None:
        problems.append(('找不到安装目录', 'npm root -g 没答上来；先确认 npm 正常'))
    elif not package_dir.exists():
        problems.append(('找不到安装目录', '%s 不存在' % package_dir))
    elif not os.access(str(package_dir), os.W_OK):
        problems.append(('安装目录不可写', '%s' % package_dir))
    free = env.free_bytes()
    if free < MIN_FREE_BYTES:
        problems.append(('磁盘不足', '可用 %.1f GB，至少需要 %.0f GB'
                         % (free / 1024 ** 3, MIN_FREE_BYTES / 1024 ** 3)))
    if target is None:
        problems.append(('目标版本不可用', '见上面版本查询的错误'))
    return problems


# ------------------------------------------------------------ 受保护资产 ----

def _excluded(relative):
    parts = Path(relative).parts
    if not parts:
        return False
    if parts[0] in DSH_HOME_EXCLUDED_TOP:
        return True
    if any(parts[0].startswith(prefix) for prefix in DSH_HOME_EXCLUDED_PREFIX):
        return True
    #: profiles/**/node_modules/**：工具为每个 profile 装依赖，升级本来就会重建。
    if parts[0] == 'profiles' and 'node_modules' in parts:
        return True
    return False


def _roots(env):
    return [
        (env.dsh_home, 'dsh-home'),
        (env.app_support / 'DeepSeek Harness', 'app-support/DeepSeek Harness'),
        (env.app_support / 'mkcert', 'app-support/mkcert'),
    ]


def inventory(env):
    """受保护资产的清单：相对路径 -> {size, mtime_ns, sha256?}。"""
    manifest = {}
    for root, prefix in _roots(env):
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            dirnames[:] = [name for name in dirnames
                           if not _excluded(os.path.relpath(os.path.join(dirpath, name),
                                                             str(root)))]
            for name in filenames:
                path = Path(dirpath) / name
                relative = os.path.join(prefix, os.path.relpath(str(path), str(root)))
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entry = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
                if stat.st_size <= HASH_LIMIT and path.is_file():
                    try:
                        entry['sha256'] = _sha256(path)
                    except OSError:
                        entry['sha256'] = 'unreadable'
                manifest[relative] = entry
    return manifest


def diff_manifest(before, after):
    """改了的/删了的/新增的，分开列。"""
    changed, removed, added = [], [], []
    for relative, info in before.items():
        current = after.get(relative)
        if current is None:
            removed.append(relative)
            continue
        if info.get('sha256') and current.get('sha256'):
            same = info['sha256'] == current['sha256']
        else:
            same = (info['size'], info['mtime_ns']) == (current['size'], current['mtime_ns'])
        if not same:
            changed.append(relative)
    for relative in after:
        if relative not in before:
            added.append(relative)
    return {'changed': sorted(changed), 'removed': sorted(removed), 'added': sorted(added)}


def repo_state(env):
    """当前仓库的 HEAD 与未提交改动；不是 git 仓库就返回 None。"""
    directory = env.repo_dir
    if not (directory / '.git').exists():
        return None
    code, out, _ = run(['git', '-C', str(directory), 'rev-parse', 'HEAD'], timeout=60)
    head = out.strip() if code == 0 else ''
    code, out, _ = run(['git', '-C', str(directory), 'status', '--porcelain'], timeout=60)
    status = out if code == 0 else ''
    return {'head': head, 'status': status}


def repo_drift(before, after):
    if before is None or after is None:
        return []
    problems = []
    if before['head'] != after['head']:
        problems.append('仓库 HEAD 变了：%s → %s' % (before['head'][:12], after['head'][:12]))
    if before['status'] != after['status']:
        problems.append('仓库工作区状态变了（升级不该碰仓库）')
    return problems


# -------------------------------------------------------------- 备份 ----

def backup_targets(env):
    """要备份的关键小文件（相对 ``dsh-home/`` / ``app-support/`` 的路径）。"""
    targets = [
        'dsh-home/.credentials.yaml',
        'dsh-home/AGENTS.md',
        'app-support/DeepSeek Harness/ai-gateway-root-ca.crt',
        'app-support/mkcert/rootCA.pem',
        'app-support/mkcert/rootCA-key.pem',
    ]
    for pattern in ('storages/*.json', 'profiles/*/config*', 'install-packs/*/*.mjs',
                    'install-packs/*/*.sh'):
        targets.extend('dsh-home/%s' % os.path.relpath(str(path), str(env.dsh_home))
                       for path in sorted(env.dsh_home.glob(pattern)) if path.is_file())
    return targets


def _resolve_backup_source(env, relative):
    if relative.startswith('dsh-home/'):
        return env.dsh_home / relative[len('dsh-home/'):]
    if relative.startswith('app-support/'):
        return env.app_support / relative[len('app-support/'):]
    raise ValueError('未知的备份根：%s' % relative)


def make_backup(env, stamp):
    """把关键小文件按相对路径复制进备份目录（目录 0700、文件 0600）。返回备份根。"""
    root = env.backups_dir / stamp
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(root), 0o700)
    saved = []
    for relative in backup_targets(env):
        source = _resolve_backup_source(env, relative)
        if not source.is_file():
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(str(source), str(destination))
        os.chmod(str(destination), 0o600)
        saved.append(relative)
    return root, saved


def restore_backup(env, root):
    """把备份文件覆盖回去。返回恢复的文件数；单个文件失败会记进 errors 但不中断。"""
    restored, errors = 0, []
    if not root or not Path(root).is_dir():
        return restored, ['备份目录不存在：%s' % root]
    for relative in backup_targets(env):
        source = Path(root) / relative
        if not source.is_file():
            continue
        destination = _resolve_backup_source(env, relative)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
            restored += 1
        except OSError as error:
            errors.append('%s：%s' % (relative, error))
    return restored, errors


# -------------------------------------------------------------- 状态 ----

def read_state(env):
    try:
        return json.loads(env.state_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write_state(env, payload):
    env.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(env.state_dir), 0o700)
    env.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                              encoding='utf-8')
    os.chmod(str(env.state_file), 0o600)


def audit(env, action, **fields):
    """追加一行审计。审计本身失败不掩盖主流程的结果，但要吼一声。"""
    record = {'ts': _now(), 'action': action}
    #: ``from`` 是 Python 关键字，调用处只能写 ``frm=``；落到审计文件里必须是 ``from``（给上层脚本用）。
    record.update({'from' if key == 'frm' else key: value for key, value in fields.items()})
    try:
        env.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(env.state_dir), 0o700)
        with open(str(env.audit_file), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        os.chmod(str(env.audit_file), 0o600)
    except OSError as error:
        print('（审计写入失败：%s）' % error, file=sys.stderr)


# -------------------------------------------------------------- 报告 ----

class Report:
    """人话 + JSON 双通道输出。`--json` 时只吐 JSON，别的打印一律闭嘴。"""

    def __init__(self, as_json):
        self.as_json = as_json
        self.lines = []
        self.payload = {}

    def say(self, text=''):
        if not self.as_json:
            print(text)

    def note(self, text):
        self.lines.append(text)

    def finish(self, payload, code):
        self.payload = payload
        self.payload.setdefault('exit_code', code)
        if self.as_json:
            print(json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True))
        return code


def _print_problems(report, problems):
    report.say('体检没过，什么都没动：')
    for name, detail in problems:
        report.say('  ✗ %s' % name)
        for line in str(detail).splitlines():
            report.say('    %s' % line)


def _confirm(report, text):
    if report.as_json:
        report.say('（--json 模式必须显式给 --yes）')
        return False
    try:
        answer = input('%s（输入 yes 继续）：' % text).strip()
    except EOFError:
        return False
    return answer == 'yes'


# -------------------------------------------------------------- 主流程 ----

def installed_version(env):
    code, out, err = run([env.dsh, '--version'], timeout=120)
    if code != 0:
        return None, (err.strip() or out.strip() or 'dsh --version rc=%d' % code)
    return out.strip().splitlines()[0].strip() if out.strip() else None, None


def isolated_smoke(env, target):
    """用新包自己的 ``lib/bin.js`` 起一次，确认这个版本真的能跑。"""
    package_dir = env.package_dir()
    if package_dir is None:
        return False, '找不到安装目录'
    binary = package_dir / 'lib' / 'bin.js'
    if not binary.is_file():
        return False, '新包里没有 lib/bin.js：%s' % binary
    code, out, err = run([env.node, str(binary), '--version'], timeout=180)
    if code != 0:
        return False, '隔离冒烟失败：%s' % (err.strip() or out.strip() or 'rc=%d' % code)
    got = out.strip().splitlines()[0].strip() if out.strip() else ''
    if got != target:
        return False, '隔离冒烟答的是 %r，不是 %s' % (got, target)
    return True, got


def npm_install(env, version, allow_scripts):
    command = [env.npm, 'i', '-g', '%s@%s' % (PACKAGE, version)]
    if allow_scripts:
        command.append('--allow-scripts=%s' % ALLOW_SCRIPTS)
    return run(command, timeout=1800)


def print_install_warnings(output):
    """npm 的 install-scripts 警告原样打出来——这次事故里就是它被埋掉的。"""
    lines = [line for line in output.splitlines() if 'install-scripts' in line]
    if not lines:
        return
    # 走 stderr：``--json`` 模式下 stdout 只许有那一个 JSON。
    print('注意：npm 没有执行这些安装脚本：', file=sys.stderr)
    for line in lines:
        print('  ' + line.strip(), file=sys.stderr)
    print('  如需补装：npm i -g --allow-scripts=%s %s@<版本>' % (ALLOW_SCRIPTS, PACKAGE),
          file=sys.stderr)


def do_upgrade(env, report, target, allow_scripts, assume_yes):
    previous, error = installed_version(env)
    if error:
        report.say('读不到当前版本：%s' % error)
        return report.finish({'action': 'upgrade', 'ok': False, 'reason': 'no-current-version'}, EXIT_REJECTED)
    if previous == target:
        report.say('已经是 %s，不用升级。' % target)
        return report.finish({'action': 'upgrade', 'ok': True, 'already': True,
                              'from': previous, 'to': target}, EXIT_OK)

    package_dir = env.package_dir()
    stamp = time.strftime('%Y%m%dT%H%M%S')
    report.say('计划：')
    report.say('  当前版本：%s    目标版本：%s' % (previous, target))
    report.say('  安装目录：%s' % package_dir)
    report.say('  备份目录：%s' % (env.backups_dir / stamp))
    report.say('  将执行：npm i -g %s@%s%s' % (PACKAGE, target, ' --allow-scripts=…' if allow_scripts else ''))
    if not assume_yes and not _confirm(report, '确认升级？'):
        report.say('已取消，什么都没动。')
        return report.finish({'action': 'upgrade', 'ok': False, 'reason': 'cancelled'}, EXIT_REJECTED)

    # 1) 快照：资产、仓库、备份、状态（状态先写 pending，崩了也能 --rollback）
    before_manifest = inventory(env)
    before_repo = repo_state(env)
    backup_root, saved = make_backup(env, stamp)
    write_state(env, {'status': 'pending', 'previous': previous, 'target': target,
                      'backup': str(backup_root), 'started_at': _now()})
    report.say('已备份 %d 个关键文件 → %s' % (len(saved), backup_root))

    # 2) 安装
    code, out, err = npm_install(env, target, allow_scripts)
    print_install_warnings(out + '\n' + err)
    if code != 0:
        report.say('npm 安装失败（rc=%d）：%s' % (code, (err or out).strip()[:400]))
        rolled, detail = rollback(env, previous, backup_root)
        audit(env, 'upgrade', ok=False, frm=previous, to=target, reason='npm-install-failed',
              asset_diff=None, repo_state=None)
        return report.finish({'action': 'upgrade', 'ok': False, 'from': previous, 'to': target,
                              'reason': 'npm-install-failed', 'rolled_back': rolled,
                              'rollback_detail': detail}, EXIT_ROLLED_BACK if rolled else EXIT_ROLLBACK_FAILED)

    # 3) 三项验证
    problems = []
    got, version_error = installed_version(env)
    if version_error or got != target:
        problems.append('dsh --version 答 %r（应为 %s）%s' % (got, target,
                                                              '：' + version_error if version_error else ''))
    smoke_ok, smoke_detail = isolated_smoke(env, target)
    if not smoke_ok:
        problems.append('隔离冒烟：%s' % smoke_detail)
    after_manifest = inventory(env)
    asset_diff = diff_manifest(before_manifest, after_manifest)
    if asset_diff['changed'] or asset_diff['removed']:
        problems.append('受保护资产被动过：改了 %d、删了 %d（%s）'
                        % (len(asset_diff['changed']), len(asset_diff['removed']),
                           ', '.join((asset_diff['changed'] + asset_diff['removed'])[:5])))
    drift = repo_drift(before_repo, repo_state(env))
    problems.extend(drift)

    if problems:
        report.say('升级后验证没过：')
        for problem in problems:
            report.say('  ✗ %s' % problem)
        rolled, detail = rollback(env, previous, backup_root)
        audit(env, 'upgrade', ok=False, frm=previous, to=target, reason='verify-failed',
              asset_diff=asset_diff, repo_state=drift)
        return report.finish({'action': 'upgrade', 'ok': False, 'from': previous, 'to': target,
                              'reason': 'verify-failed', 'problems': problems,
                              'asset_diff': asset_diff, 'rolled_back': rolled,
                              'rollback_detail': detail}, EXIT_ROLLED_BACK if rolled else EXIT_ROLLBACK_FAILED)

    # 4) 成功：状态记成 completed（含受保护资产的新增项，仅供审计）
    write_state(env, {'status': 'completed', 'previous': previous, 'target': target,
                      'backup': str(backup_root), 'finished_at': _now()})
    audit(env, 'upgrade', ok=True, frm=previous, to=target, reason='ok',
          asset_diff={'added': asset_diff['added'], 'changed': [], 'removed': []},
          repo_state=[])
    report.say('升级完成：%s → %s' % (previous, target))
    if asset_diff['added']:
        report.say('（受保护资产新增 %d 项，属正常：工作台自己会写会话/缓存之外的少量文件）' % len(asset_diff['added']))
    report.say('回滚命令：python3 tools/dsh-upgrade.py --rollback')
    return report.finish({'action': 'upgrade', 'ok': True, 'from': previous, 'to': target,
                          'asset_diff': asset_diff, 'backup': str(backup_root),
                          'verified': {'version': got, 'smoke': smoke_detail}}, EXIT_OK)


def rollback(env, previous, backup_root):
    """退回旧版本 + 恢复备份。返回 (是否成功, 说明)。"""
    print('回滚中：装回 %s 并恢复备份…' % previous, file=sys.stderr)
    code, out, err = npm_install(env, previous, allow_scripts=False)
    restored, errors = restore_backup(env, backup_root)
    got, version_error = installed_version(env)
    ok = code == 0 and not errors and got == previous
    detail = {'npm_rc': code, 'restored': restored, 'restore_errors': errors,
              'version_now': got, 'version_error': version_error}
    if not ok:
        print('回滚没成功，需要人工恢复：', file=sys.stderr)
        print('  1) npm i -g %s@%s' % (PACKAGE, previous), file=sys.stderr)
        print('  2) 用备份覆盖回去：%s' % backup_root, file=sys.stderr)
        print('  3) 核对 dsh --version 与受保护资产，再起 GUI', file=sys.stderr)
    return ok, detail


def do_rollback(env, report, assume_yes):
    state = read_state(env)
    if not state or state.get('status') not in ('completed', 'pending'):
        report.say('没有可用的升级记录（%s），不能 --rollback。' % env.state_file)
        audit(env, 'rollback', ok=False, reason='no-state')
        return report.finish({'action': 'rollback', 'ok': False, 'reason': 'no-state'}, EXIT_REJECTED)
    target = state.get('previous')
    backup_root = state.get('backup')
    if not target:
        report.say('状态文件里没有上一版本号，不能 --rollback。')
        audit(env, 'rollback', ok=False, reason='no-previous')
        return report.finish({'action': 'rollback', 'ok': False, 'reason': 'no-previous'}, EXIT_REJECTED)
    report.say('回滚计划：%s → %s（备份 %s）' % (state.get('target'), target, backup_root))
    if not assume_yes and not _confirm(report, '确认回滚？'):
        report.say('已取消。')
        return report.finish({'action': 'rollback', 'ok': False, 'reason': 'cancelled'}, EXIT_REJECTED)
    ok, detail = rollback(env, target, backup_root)
    if ok:
        write_state(env, {'status': 'rolled-back', 'previous': state.get('previous'),
                          'target': state.get('target'), 'backup': backup_root,
                          'finished_at': _now()})
    audit(env, 'rollback', ok=ok, frm=state.get('target'), to=target, reason='ok' if ok else 'failed')
    return report.finish({'action': 'rollback', 'ok': ok, 'from': state.get('target'), 'to': target,
                          'detail': detail}, EXIT_OK if ok else EXIT_ROLLBACK_FAILED)


def do_check(env, report, wanted):
    """体检：只读。零写入（连审计都不写）。"""
    target, error, versions = resolve_target(env, wanted) if wanted else (None, None, [])
    previous, version_error = installed_version(env)
    problems = preflight(env, target if wanted else (previous or 'n/a'))
    if wanted and error:
        problems.append(('目标版本不可用', error + '\n  → 最近版本：%s' % ', '.join(versions[-5:])))
    manifest = inventory(env)
    running, reasons = host_running(env)
    state = read_state(env)
    payload = {
        'action': 'check',
        'ok': not problems,
        'current_version': previous,
        'current_version_error': version_error,
        'target_version': target,
        'host_running': running,
        'host_reasons': reasons,
        'recent_session_writes': [path for path, _ in recent_session_writes(env)[:5]],
        'protected_assets': len(manifest),
        'free_bytes': env.free_bytes(),
        'state': state,
        'problems': [{'name': name, 'detail': detail} for name, detail in problems],
    }
    report.say('当前版本：%s' % previous)
    if wanted:
        report.say('目标版本：%s' % target)
    report.say('宿主在跑：%s' % ('是 —— 不能升级' if running else '否'))
    report.say('最近 5 分钟内写过会话：%d 个' % len(payload['recent_session_writes']))
    report.say('受保护资产：%d 项' % len(manifest))
    report.say('可用空间：%.1f GB' % (env.free_bytes() / 1024 ** 3))
    if state:
        report.say('上次升级记录：%s → %s（%s）' % (state.get('previous'), state.get('target'),
                                                state.get('status')))
    if problems:
        _print_problems(report, problems)
    else:
        report.say('体检通过，可以升级。')
    return report.finish(payload, EXIT_OK if not problems else EXIT_REJECTED)


def do_dry_run(env, report, wanted, allow_scripts):
    """打印完整计划：备份哪些、执行什么、验什么。零写入。"""
    target, error, versions = resolve_target(env, wanted)
    previous, version_error = installed_version(env)
    problems = preflight(env, target)
    if error:
        problems.append(('目标版本不可用', error + '\n  → 最近版本：%s' % ', '.join(versions[-5:])))
    stamp = time.strftime('%Y%m%dT%H%M%S')
    backup_root = env.backups_dir / stamp
    targets = [relative for relative in backup_targets(env)
               if _resolve_backup_source(env, relative).is_file()]
    payload = {
        'action': 'dry-run',
        'ok': not problems,
        'current_version': previous,
        'current_version_error': version_error,
        'target_version': target,
        'install_dir': str(env.package_dir() or ''),
        'backup_root': str(backup_root),
        'backup_files': targets,
        'commands': [
            ' '.join(npm_install_command(env, target, allow_scripts)),
            '%s --version（期望 %s）' % (env.dsh, target),
            '%s %s/lib/bin.js --version（隔离冒烟，期望 %s）'
            % (env.node, env.package_dir() or '<安装目录>', target),
            '比对 %d 项受保护资产 + 仓库 HEAD/状态' % len(inventory(env)),
        ],
        'problems': [{'name': name, 'detail': detail} for name, detail in problems],
    }
    report.say('计划（dry-run，不执行）：')
    report.say('  当前 → 目标：%s → %s' % (previous, target))
    report.say('  备份到：%s' % backup_root)
    report.say('  备份文件 %d 个：' % len(targets))
    for relative in targets:
        report.say('    - %s' % relative)
    report.say('  将执行：')
    for command in payload['commands']:
        report.say('    $ %s' % command)
    if problems:
        _print_problems(report, problems)
    return report.finish(payload, EXIT_OK if not problems else EXIT_REJECTED)


def npm_install_command(env, target, allow_scripts):
    command = [env.npm, 'i', '-g', '%s@%s' % (PACKAGE, target or '<版本>')]
    if allow_scripts:
        command.append('--allow-scripts=%s' % ALLOW_SCRIPTS)
    return command


def build_parser():
    parser = argparse.ArgumentParser(
        prog='dsh-upgrade.py',
        description='安全升级 DSH（停 → 装 → 起 → 验证，带受保护资产比对与自动回滚）。\n'
                    '退出码：0 成功 / 2 拒绝 / 3 失败已回滚 / 4 回滚失败',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--to', metavar='版本', help='目标版本，或 latest')
    parser.add_argument('--check', action='store_true', help='只体检：零写入（含审计）')
    parser.add_argument('--dry-run', action='store_true', help='打印完整计划，不执行、不写文件')
    parser.add_argument('--rollback', action='store_true', help='按状态文件回到上一版本')
    parser.add_argument('--yes', action='store_true', help='非交互确认')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    parser.add_argument('--allow-scripts', action='store_true',
                        help='允许 npm 执行安装脚本（native 模块需要时）')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    env = Environment()
    report = Report(args.json)
    try:
        if args.check:
            return do_check(env, report, args.to)
        if args.dry_run:
            return do_dry_run(env, report, args.to, args.allow_scripts)
        if args.rollback:
            return do_rollback(env, report, args.yes)

        # 真升级：先体检，任何一条不过就原地拒绝（并留审计）
        target, error, versions = resolve_target(env, args.to)
        problems = preflight(env, target)
        if error:
            problems.append(('目标版本不可用', error + '\n  → 最近版本：%s'
                             % ', '.join(versions[-5:]) if versions else '  → npm 上没有版本'))
        if problems:
            _print_problems(report, problems)
            audit(env, 'upgrade', ok=False, reason='preflight-failed',
                  frm=None, to=args.to,
                  problems=[name for name, _ in problems])
            return report.finish({'action': 'upgrade', 'ok': False,
                                  'reason': 'preflight-failed',
                                  'problems': [{'name': name, 'detail': detail}
                                               for name, detail in problems]}, EXIT_REJECTED)
        return do_upgrade(env, report, target, args.allow_scripts, args.yes)
    except Exception as error:  # noqa: BLE001 - 脚本的兜底：不许裸 traceback 加半个升级
        traceback.print_exc()
        audit(env, 'error', ok=False, reason='exception: %s' % error)
        return report.finish({'action': 'error', 'ok': False, 'reason': str(error)}, EXIT_ROLLBACK_FAILED)


if __name__ == '__main__':
    sys.exit(main())
