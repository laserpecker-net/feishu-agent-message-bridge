"""``tools/dsh-upgrade.py`` 的判据。

事故背景（``AGENTS.md`` §8）：2026-11-18 在宿主跑着的时候就地 ``npm i -g``，把所有会话弄崩。
这个脚本的职责是**让人做不出那件错事**，所以测试的重点不是"升级成功"，而是：

1. 挡住了没有（宿主在跑、有人刚在写会话、版本不存在、磁盘不够）
2. 挡住了以后有没有说清下一步
3. 万一放行了、中途有东西被动了（凭证/配置/仓库），**能不能自己退回去**
4. 只写一个目录（除 npm 全局安装外）——这条用"临时 home 全量快照"钉住

全部用临时 home + 假 ``npm``/``dsh``/``node``，**绝不碰真实安装**。
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / 'tools' / 'dsh-upgrade.py'

FAKE_NPM = r'''#!%(python)s
import json, os, pathlib, sys

args = sys.argv[1:]
log = os.environ.get('FAKE_NPM_LOG')
if log:
    with open(log, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(args) + '\n')


def die(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def version_file():
    return pathlib.Path(os.environ['FAKE_VERSION_FILE'])


if args[:1] == ['view']:
    if 'versions' in args:
        print(os.environ['FAKE_VERSIONS'])
        sys.exit(0)
    if 'dist-tags' in args:
        tags = json.loads(os.environ['FAKE_VERSIONS'])
        print(json.dumps({'latest': tags[-1]}))
        sys.exit(0)
    die('unsupported view: %%r' %% (args,))


if args[:2] == ['root', '-g']:
    print(str(pathlib.Path(os.environ['FAKE_INSTALL_DIR']).parent))
    sys.exit(0)


if args[:2] in (['i', '-g'], ['install', '-g']):
    specs = [item for item in args if item.startswith('@deepseek-ai/dsh@')]
    if not specs:
        die('fake npm: 没带版本')
    version = specs[-1].rsplit('@', 1)[-1]
    if os.environ.get('FAKE_NPM_FAIL_VERSION') == version:
        die('fake npm: 装 %%s 失败' %% version)
    version_file().write_text(version, encoding='utf-8')
    side = os.environ.get('FAKE_SIDE_EFFECT')
    home = pathlib.Path(os.environ['FAKE_DSH_HOME'])
    if side == 'corrupt-credentials':
        (home / '.credentials.yaml').write_text('corrupted by npm\n', encoding='utf-8')
    elif side == 'delete-agents':
        (home / 'AGENTS.md').unlink(missing_ok=True)   # 回滚时再装一次，已经没了也不算错
    elif side == 'touch-repo':
        (pathlib.Path(os.environ['FAKE_REPO_DIR']) / 'npm-touched.txt').write_text('x', encoding='utf-8')
    elif side == 'add-session':
        (home / 'sessions' / 'proj' / 'new.jsonl').write_text('{}', encoding='utf-8')
    sys.exit(0)


die('fake npm: 不认识的参数 %%r' %% (args,), 2)
'''

FAKE_DSH = r'''#!%(python)s
import os, pathlib, sys

if sys.argv[1:] == ['--version']:
    path = pathlib.Path(os.environ['FAKE_VERSION_FILE'])
    print(path.read_text(encoding='utf-8').strip() if path.exists() else '')
    sys.exit(0)
print('fake dsh')
sys.exit(0)
'''

FAKE_NODE = r'''#!%(python)s
import os, pathlib, sys

if '--version' in sys.argv:
    path = pathlib.Path(os.environ['FAKE_VERSION_FILE'])
    print(path.read_text(encoding='utf-8').strip() if path.exists() else '')
    sys.exit(0)
sys.exit(0)
'''

CURRENT = '0.2.1-rc.5'
TARGET = '0.2.2-rc.4'
VERSIONS = ['0.2.1-rc.4', '0.2.1-rc.5', '0.2.2-rc.3', '0.2.2-rc.4']


def _write_executable(path, body):
    path.write_text(body, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def snapshot(root, skip=('Library/Caches/',)):
    """{相对路径: (size, mtime_ns) 或 None（目录）}，用来钉"什么都没动"。

    ``skip`` 默认跳过 ``Library/Caches/``：那是 Python 解释器自己的字节码缓存目录，
    不是本工具写的（假 npm/dsh/node 也是 python 脚本，会把 .pyc 落在这里）。
    """
    entries = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames:
            relative = os.path.relpath(os.path.join(dirpath, name), str(root))
            if any((relative + '/').startswith(prefix) for prefix in skip):
                continue
            entries[relative + '/'] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            relative = os.path.relpath(path, str(root))
            if any(relative.startswith(prefix) for prefix in skip):
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            entries[relative] = (info.st_size, info.st_mtime_ns)
    return entries


class UpgradeCase(unittest.TestCase):
    """一套临时 home：假 npm/dsh/node + 受保护资产 + 一个真 git 仓库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dsh-upgrade-test-'))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = self.tmp / 'home'
        self.dsh_home = self.home / '.dsh'
        self.support = self.home / 'Library' / 'Application Support'
        self.state_dir = self.support / 'dsh-upgrade'
        self.bin = self.tmp / 'bin'
        self.install_dir = self.tmp / 'npm-root' / '@deepseek-ai' / 'dsh'
        self.version_file = self.tmp / 'fake' / 'installed-version'
        self.npm_log = self.tmp / 'fake' / 'npm.log'
        self.repo = self.tmp / 'repo'
        python = sys.executable or '/usr/bin/python3'

        for directory in (self.dsh_home / 'storages', self.dsh_home / 'profiles' / 'p1',
                          self.dsh_home / 'sessions' / 'proj',
                          self.dsh_home / 'install-packs' / 'pack',
                          self.support / 'DeepSeek Harness', self.support / 'mkcert',
                          self.bin, self.install_dir / 'lib', self.version_file.parent):
            directory.mkdir(parents=True, exist_ok=True)

        (self.dsh_home / '.credentials.yaml').write_text('authorization:\n  enabled: true\n',
                                                         encoding='utf-8')
        (self.dsh_home / 'AGENTS.md').write_text('# 全局规则\n', encoding='utf-8')
        (self.dsh_home / 'storages' / 's1.json').write_text('{"a": 1}', encoding='utf-8')
        (self.dsh_home / 'profiles' / 'p1' / 'config.json').write_text('{"b": 2}', encoding='utf-8')
        (self.dsh_home / 'install-packs' / 'pack' / 'launch-dsh.mjs').write_text('// launcher\n',
                                                                                encoding='utf-8')
        session = self.dsh_home / 'sessions' / 'proj' / 'session.jsonl'
        session.write_text('{}\n', encoding='utf-8')
        old = time.time() - 7200
        os.utime(str(session), (old, old))
        os.utime(str(session.parent), (old, old))
        (self.support / 'DeepSeek Harness' / 'ai-gateway-root-ca.crt').write_text('CERT\n',
                                                                                 encoding='utf-8')
        (self.support / 'mkcert' / 'rootCA.pem').write_text('ROOT\n', encoding='utf-8')
        (self.support / 'mkcert' / 'rootCA-key.pem').write_text('KEY\n', encoding='utf-8')
        (self.install_dir / 'lib' / 'bin.js').write_text('// dsh bin\n', encoding='utf-8')
        self.version_file.write_text(CURRENT, encoding='utf-8')

        _write_executable(self.bin / 'npm', FAKE_NPM % {'python': python})
        _write_executable(self.bin / 'dsh', FAKE_DSH % {'python': python})
        _write_executable(self.bin / 'node', FAKE_NODE % {'python': python})

        self.repo.mkdir()
        self._git('init', '-q')
        self._git('config', 'user.email', 'test@example.invalid')
        self._git('config', 'user.name', 'test')
        (self.repo / 'README.md').write_text('repo\n', encoding='utf-8')
        self._git('add', 'README.md')
        self._git('commit', '-q', '-m', 'init')

    # -- helpers -----------------------------------------------------------
    def _git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo)] + list(args),
                              capture_output=True, text=True, check=False)

    def env(self, **overrides):
        env = dict(os.environ)
        env.pop('DSH_UPGRADE_FAKE_HOST', None)
        env.update({
            'HOME': str(self.home),
            'DSH_HOME': str(self.dsh_home),
            'DSH_UPGRADE_APP_SUPPORT': str(self.support),
            'DSH_UPGRADE_STATE_DIR': str(self.state_dir),
            'DSH_UPGRADE_NPM': str(self.bin / 'npm'),
            'DSH_UPGRADE_DSH': str(self.bin / 'dsh'),
            'DSH_UPGRADE_NODE': str(self.bin / 'node'),
            'DSH_UPGRADE_INSTALL_DIR': str(self.install_dir),
            'DSH_UPGRADE_REPO_DIR': str(self.repo),
            'DSH_UPGRADE_PLATFORM': 'darwin',
            # 测试不许扫真实进程：'0' = 假装宿主没在跑
            'DSH_UPGRADE_FAKE_HOST': '0',
            'DSH_UPGRADE_FREE_BYTES': str(5 * 1024 ** 3),
            'FAKE_VERSION_FILE': str(self.version_file),
            'FAKE_NPM_LOG': str(self.npm_log),
            'FAKE_VERSIONS': json.dumps(VERSIONS),
            'FAKE_INSTALL_DIR': str(self.install_dir),
            'FAKE_DSH_HOME': str(self.dsh_home),
            'FAKE_REPO_DIR': str(self.repo),
            # 假脚本也是 python：别让解释器把字节码缓存写进临时 home（那不是本工具的写入）
            'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': '%s:%s' % (self.bin, env.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')),
        })
        env.update({key: str(value) for key, value in overrides.items()})
        return env

    def run_tool(self, *args, env=None):
        return subprocess.run([sys.executable, str(TOOL)] + list(args),
                              capture_output=True, text=True, cwd=str(self.repo),
                              env=env if env is not None else self.env(), timeout=180)

    def npm_calls(self):
        if not self.npm_log.exists():
            return []
        return [json.loads(line) for line in self.npm_log.read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def install_calls(self):
        return [call for call in self.npm_calls() if call[:2] in (['i', '-g'], ['install', '-g'])]

    def audit_lines(self):
        if not self.state_dir.joinpath('audit.jsonl').exists():
            return []
        return [json.loads(line) for line in
                self.state_dir.joinpath('audit.jsonl').read_text(encoding='utf-8').splitlines()
                if line.strip()]


class PreflightTests(UpgradeCase):

    def test_host_running_is_rejected(self):
        result = self.run_tool('--to', TARGET, '--yes', env=self.env(DSH_UPGRADE_FAKE_HOST='1'))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('宿主在跑', result.stdout)
        self.assertIn('先停 GUI', result.stdout)
        self.assertEqual(self.install_calls(), [])           # 一个字都没动
        # 被拒绝也要留审计（AGENTS §8 的教训：拒绝本身是证据），但**不许**有备份/状态文件
        self.assertEqual(sorted(path.name for path in self.state_dir.iterdir()), ['audit.jsonl'])

    def test_recent_session_write_is_rejected_then_allowed(self):
        session = self.dsh_home / 'sessions' / 'proj' / 'session.jsonl'
        session.write_text('{"fresh": 1}\n', encoding='utf-8')
        recent = self.run_tool('--check', '--to', TARGET)
        self.assertEqual(recent.returncode, 2, recent.stdout)
        self.assertIn('有活跃会话', recent.stdout)

        old = time.time() - 3600
        os.utime(str(session), (old, old))
        for child in (self.dsh_home / 'sessions').rglob('*'):
            os.utime(str(child), (old, old))
        os.utime(str(self.dsh_home / 'sessions'), (old, old))
        os.utime(str(self.dsh_home / 'sessions' / 'proj'), (old, old))
        allowed = self.run_tool('--check', '--to', TARGET)
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_unknown_version_is_rejected_with_candidates(self):
        result = self.run_tool('--to', '9.9.9', '--yes')
        self.assertEqual(result.returncode, 2)
        self.assertIn('9.9.9', result.stdout + result.stderr)
        for version in VERSIONS[-3:]:
            self.assertIn(version, result.stdout + result.stderr)

    def test_low_disk_is_rejected(self):
        result = self.run_tool('--to', TARGET, '--yes',
                               env=self.env(DSH_UPGRADE_FREE_BYTES=str(1024 ** 3)))
        self.assertEqual(result.returncode, 2)
        self.assertIn('磁盘不足', result.stdout)

    def test_preflight_rejection_is_audited(self):
        result = self.run_tool('--to', TARGET, '--yes',
                               env=self.env(DSH_UPGRADE_FREE_BYTES=str(1024 ** 3)))
        self.assertEqual(result.returncode, 2)
        records = self.audit_lines()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['action'], 'upgrade')
        self.assertFalse(records[0]['ok'])


class NoWriteTests(UpgradeCase):

    def _assert_no_writes(self, result):
        self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
        self.assertFalse(self.state_dir.exists(), '--check/--dry-run 不许建 state 目录')
        self.assertEqual(self.install_calls(), [])

    def test_check_writes_nothing(self):
        before = snapshot(self.home)
        result = self.run_tool('--check', '--to', TARGET)
        self._assert_no_writes(result)
        self.assertEqual(before, snapshot(self.home))
        self.assertEqual(self.version_file.read_text().strip(), CURRENT)

    def test_dry_run_writes_nothing(self):
        before = snapshot(self.home)
        result = self.run_tool('--dry-run', '--to', TARGET)
        self._assert_no_writes(result)
        self.assertEqual(before, snapshot(self.home))
        self.assertIn('备份文件', result.stdout)
        self.assertIn('npm i -g @deepseek-ai/dsh@%s' % TARGET, result.stdout)


class UpgradeTests(UpgradeCase):

    def test_successful_upgrade(self):
        result = self.run_tool('--to', TARGET, '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.install_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('@deepseek-ai/dsh@%s' % TARGET, calls[0])
        self.assertEqual(self.version_file.read_text().strip(), TARGET)

        state = json.loads(self.state_dir.joinpath('state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['previous'], CURRENT)
        self.assertEqual(state['target'], TARGET)

        backups = list(self.state_dir.joinpath('backups').rglob('*'))
        files = [path for path in backups if path.is_file()]
        self.assertTrue(files, '必须真的备份了东西')
        for path in files:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, str(path))

        records = self.audit_lines()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['action'], 'upgrade')
        self.assertTrue(records[0]['ok'])
        self.assertEqual(records[0]['from'], CURRENT)
        self.assertEqual(records[0]['to'], TARGET)

    def test_protected_asset_change_triggers_rollback(self):
        env = self.env(FAKE_SIDE_EFFECT='corrupt-credentials')
        original = (self.dsh_home / '.credentials.yaml').read_text(encoding='utf-8')
        result = self.run_tool('--to', TARGET, '--yes', env=env)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('受保护资产被动过', result.stdout)
        calls = self.install_calls()
        self.assertEqual(len(calls), 2, calls)               # 先装新、再装回旧
        self.assertIn('@deepseek-ai/dsh@%s' % TARGET, calls[0])
        self.assertIn('@deepseek-ai/dsh@%s' % CURRENT, calls[1])
        self.assertEqual((self.dsh_home / '.credentials.yaml').read_text(encoding='utf-8'), original)
        self.assertEqual(self.version_file.read_text().strip(), CURRENT)

    def test_removed_asset_triggers_rollback(self):
        env = self.env(FAKE_SIDE_EFFECT='delete-agents')
        result = self.run_tool('--to', TARGET, '--yes', env=env)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertTrue((self.dsh_home / 'AGENTS.md').exists())
        self.assertIn('AGENTS.md', result.stdout)

    def test_repo_drift_triggers_rollback(self):
        result = self.run_tool('--to', TARGET, '--yes', env=self.env(FAKE_SIDE_EFFECT='touch-repo'))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('仓库', result.stdout)
        self.assertEqual(self.version_file.read_text().strip(), CURRENT)

    def test_new_asset_is_only_a_warning(self):
        result = self.run_tool('--to', TARGET, '--yes', env=self.env(FAKE_SIDE_EFFECT='add-session'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('受保护资产新增', result.stdout)
        self.assertTrue((self.dsh_home / 'sessions' / 'proj' / 'new.jsonl').exists())

    def test_rollback_failure_exits_four(self):
        env = self.env(FAKE_SIDE_EFFECT='corrupt-credentials', FAKE_NPM_FAIL_VERSION=CURRENT)
        result = self.run_tool('--to', TARGET, '--yes', env=env)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn('人工恢复', result.stderr)
        self.assertIn('npm i -g @deepseek-ai/dsh@%s' % CURRENT, result.stderr)

    def test_installed_version_must_match_exactly(self):
        env = self.env(FAKE_NPM_FAIL_VERSION=TARGET)          # 安装直接失败
        result = self.run_tool('--to', TARGET, '--yes',
                               env=dict(env, FAKE_NPM_FAIL_VERSION=TARGET))
        self.assertIn(result.returncode, (3, 4))
        self.assertIn('npm 安装失败', result.stdout)


class RollbackCommandTests(UpgradeCase):

    def test_rollback_without_record_is_rejected(self):
        result = self.run_tool('--rollback', '--yes')
        self.assertEqual(result.returncode, 2)
        self.assertIn('没有可用的升级记录', result.stdout)
        self.assertEqual(self.install_calls(), [])

    def test_rollback_with_record_installs_previous(self):
        first = self.run_tool('--to', TARGET, '--yes')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.version_file.read_text().strip(), TARGET)

        self.npm_log.unlink()
        second = self.run_tool('--rollback', '--yes')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        calls = self.install_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('@deepseek-ai/dsh@%s' % CURRENT, calls[0])
        self.assertEqual(self.version_file.read_text().strip(), CURRENT)


class JsonAndScopeTests(UpgradeCase):

    def test_json_output_is_parseable_on_reject(self):
        result = self.run_tool('--json', '--to', '9.9.9', '--yes')
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['exit_code'], 2)
        self.assertFalse(payload['ok'])

    def test_json_output_is_parseable_on_success(self):
        result = self.run_tool('--json', '--to', TARGET, '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['from'], CURRENT)
        self.assertEqual(payload['to'], TARGET)

    def test_writes_only_the_state_dir(self):
        before = snapshot(self.tmp)

        allowed_prefixes = [
            str(self.state_dir.relative_to(self.tmp)) + '/',
            str(self.install_dir.relative_to(self.tmp)) + '/',
            str(self.version_file.relative_to(self.tmp)),
            str(self.npm_log.relative_to(self.tmp)),
            str((self.repo / '.git').relative_to(self.tmp)) + '/',   # git status 会碰 index
        ]

        def allowed(relative):
            return any(relative.startswith(prefix) for prefix in allowed_prefixes)

        result = self.run_tool('--to', TARGET, '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = snapshot(self.tmp)
        touched = sorted(relative for relative in set(before) | set(after)
                         if before.get(relative) != after.get(relative) and not allowed(relative))
        self.assertEqual(touched, [], '升级只许写 state 目录与 npm 安装目录')


class HostDetectionTests(unittest.TestCase):
    """第一道门必须认全三种起法——2026-11-18 那次就是漏了"启动器形态"。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('dsh_upgrade', TOOL)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_recognizes_all_three_launch_forms(self):
        samples = [
            'npm exec @deepseek-ai/dsh web',
            '/opt/node/bin/node /opt/npm-global/lib/node_modules/@deepseek-ai/dsh/lib/bin.js web',
            '/opt/node/bin/node /opt/dsh-home/install-packs/dsh-models/launch-dsh.mjs '
            '--bin /opt/npm-global/lib/node_modules/@deepseek-ai/dsh/lib/bin.js web',
            'dsh web --port 8765',
        ]
        for args in samples:
            self.assertTrue(self.module._looks_like_host(args), args)

    def test_ignores_unrelated_processes(self):
        samples = [
            'python3 tools/dsh-upgrade.py --check',
            'node /opt/work/index.js --web',
            'dsh headless --session-id abc',
            'npm exec @deepseek-ai/dsh headless --json -',
        ]
        for args in samples:
            self.assertFalse(self.module._looks_like_host(args), args)

    def test_port_probe_sees_a_listener(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(self.module._port_open(port))
        self.assertFalse(self.module._port_open(port))


if __name__ == '__main__':
    unittest.main()
