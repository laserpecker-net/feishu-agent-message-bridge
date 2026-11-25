import contextlib
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import install

SOURCE_FILES = {
    '__init__.py': '',
    'agent.py': 'import os\n',
    'cli.py': 'from . import service\n',
    'service.py': 'import json\n',
    'store.py': 'import sqlite3\n',
    'transport.py': 'import subprocess\n',
}


def make_source(root):
    bridge = Path(root) / 'bridge'
    bridge.mkdir(parents=True)
    for name, text in SOURCE_FILES.items():
        (bridge / name).write_text(text)
    # 消费者运行时随每个 release 一起发（install.CONSUMER_FILES），源码树里必须有。
    for name in install.CONSUMER_FILES:
        (Path(root) / name).write_text('# %s\n' % name)
    # 身份 sidecar（as_bot 的长连接收消息器）同样随 release 走：install 会复制 sidecar/。
    sidecar = Path(root) / install.IDENTITY_SIDECAR_DIR
    sidecar.mkdir(parents=True, exist_ok=True)
    (sidecar / 'index.js').write_text('#!/usr/bin/env node\n')
    (sidecar / 'package.json').write_text('{"name": "sidecar"}\n')
    return Path(root)


class InstallFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.source = make_source(self.root / 'source')
        self.home = self.root / 'home'
        self.plist = self.root / 'LaunchAgents' / (install.LABEL + '.plist')
        self.link = self.root / 'bin' / 'feishu-bridge'
        self.lark = self.root / 'lark-cli'
        self.lark.write_text('#!/bin/sh\nexit 0\n')
        # 每台机器的主人都必须显式给出自己的 open_id（install.py 不再有静默默认值）。
        self.target = 'ou_fixture'

    def install(self, source=None, **options):
        with patch.dict(os.environ, {'TARGET_OPEN_ID': self.target}):
            return install.install(source=source or self.source, home=self.home, plist=self.plist,
                                   link=self.link, lark_binary=self.lark, **options)

    def release_path(self):
        return self.home / 'releases' / install._release_id(self.source)

    def config(self):
        return json.loads((self.home / 'config.json').read_text())

    def receipt(self):
        return json.loads((self.home / 'installation.json').read_text())


class AtomicWriteTests(unittest.TestCase):
    def test_writes_bytes_with_private_mode_and_no_temp_leftovers(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'nested' / 'config.json'
            install._atomic_write(target, b'{"a": 1}', 0o600)
            self.assertEqual(target.read_bytes(), b'{"a": 1}')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual([path.name for path in target.parent.iterdir()], ['config.json'])

    def test_uses_requested_mode(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'launcher'
            install._atomic_write(target, b'#!/bin/sh\n', 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_failed_replace_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'config.json'
            target.write_text('old')
            with patch.object(install.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    install._atomic_write(target, b'new')
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual([path.name for path in target.parent.iterdir()], ['config.json'])


class ReleaseVerifyTests(unittest.TestCase):
    def test_half_copied_release_is_rebuilt_from_source(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            release = fixture.root / 'releases' / 'any-id'
            (release / 'bridge').mkdir(parents=True)
            (release / 'bridge' / '__init__.py').write_text('stale')
            hashes = install._verify_release(fixture.source, release)
            expected = install._file_hashes(install._bridge_files(fixture.source))
            self.assertEqual(hashes, expected)
            self.assertEqual((release / 'bridge' / 'cli.py').read_text(), SOURCE_FILES['cli.py'])
            self.assertEqual((release / 'bridge' / '__init__.py').read_text(), '')

    def test_mismatched_release_file_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            release = fixture.root / 'releases' / 'any-id'
            (release / 'bridge').mkdir(parents=True)
            for name in SOURCE_FILES:
                (release / 'bridge' / name).write_text('# stale copy\n')
            install._verify_release(fixture.source, release)
            self.assertEqual((release / 'bridge' / 'store.py').read_text(), SOURCE_FILES['store.py'])

    def test_failed_copy_cleans_partial_release_and_raises(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            release = fixture.root / 'releases' / 'any-id'

            def broken_copytree(source, destination, **kwargs):
                Path(destination).mkdir(parents=True)
                (Path(destination) / 'cli.py').write_text('# partial\n')
                raise OSError('disk full')

            with patch.object(install.shutil, 'copytree', side_effect=broken_copytree):
                with self.assertRaises(OSError):
                    install._verify_release(fixture.source, release)
            self.assertFalse((release / 'bridge').exists())


class SourceGuardTests(unittest.TestCase):
    def test_refuses_coordination_copy(self):
        with tempfile.TemporaryDirectory() as root:
            source = make_source(Path(root) / 'coordination' / 'copy')
            with self.assertRaises(RuntimeError) as caught:
                install._guard_source(source)
            self.assertIn('review copy', str(caught.exception))

    def test_refuses_review_snapshot_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source = make_source(Path(root) / 'review-snapshot')
            with self.assertRaises(RuntimeError):
                install._guard_source(source)

    def test_refuses_review_repaired_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source = make_source(Path(root) / 'review-repaired')
            with self.assertRaises(RuntimeError):
                install._guard_source(source)

    def test_install_from_review_copy_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            source = make_source(fixture.root / 'coordination' / 'review-repaired')
            with self.assertRaises(RuntimeError):
                install.install(source=source, home=fixture.home, plist=fixture.plist, link=fixture.link,
                                lark_binary=fixture.lark)
            self.assertFalse(fixture.home.exists())


class ConfigDefaultsTests(unittest.TestCase):
    def test_defaults_are_added(self):
        merged = install._merge_config_defaults({'sending_enabled': True})
        for key, value in install.CONFIG_DEFAULTS.items():
            self.assertEqual(merged[key], value)
        self.assertTrue(merged['sending_enabled'])

    def test_existing_values_win(self):
        merged = install._merge_config_defaults({'app_daily_reply_cap': 5, 'live_interval_seconds': 3,
                                                 'artifact_retention_days': 0})
        self.assertEqual(merged['app_daily_reply_cap'], 5)
        self.assertEqual(merged['live_interval_seconds'], 3)
        self.assertEqual(merged['artifact_retention_days'], 0)
        self.assertEqual(merged['collector_backoff_seconds'], 10)

    def test_install_merges_defaults_into_existing_config(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.target = 'ou_existing'
            fixture.home.mkdir(parents=True)
            (fixture.home / 'config.json').write_text(json.dumps({'sending_enabled': True,
                                                                  'target_open_id': 'ou_existing',
                                                                  'live_interval_seconds': 3}))
            fixture.install()
            config = fixture.config()
            self.assertTrue(config['sending_enabled'])
            self.assertEqual(config['target_open_id'], 'ou_existing')
            self.assertEqual(config['live_interval_seconds'], 3)
            self.assertEqual(config['app_daily_reply_cap'], 20)
            self.assertEqual(config['collector_backoff_seconds'], 10)


class PruneProtectionTests(unittest.TestCase):
    """被已装服务定义引用的 release 不能被 prune 删掉（agentkit plist 是绝对路径）。"""

    def test_referenced_release_survives_pruning(self):
        import install
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            releases = home / 'releases'
            releases.mkdir()
            ids = ['a' * 16, 'b' * 16, 'c' * 16]
            for index, release_id in enumerate(ids):
                path = releases / release_id
                path.mkdir()
                (path / 'marker').write_text('x')
                os.utime(path, (1000 + index, 1000 + index))
            # 服务目录里有一个 plist 指向最老的那个 release
            service_dir = home / 'LaunchAgents'
            service_dir.mkdir()
            (service_dir / 'com.example.agentkit.plist').write_text(
                '<string>%s</string>' % (releases / ids[0] / 'agentkit-consumer.py'))
            with patch.object(install.host, 'service_dir', return_value=service_dir):
                removed = install._prune_releases(home, ids[2])
            remaining = sorted(path.name for path in releases.iterdir())
        self.assertIn(ids[0], remaining, '被服务定义引用的 release 被误删')
        self.assertNotIn('0000', removed)


class SignatureTests(unittest.TestCase):
    """署名由安装者决定：本次显式给 > 既有 config.json > 不写（沿用内置默认）。"""

    def test_signature_is_written_into_config(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.dict(os.environ, {'SIGNATURE': '来自小明的AI助手'}):
                fixture.install()
            self.assertEqual(fixture.config()['signature'], '来自小明的AI助手')

    def test_existing_signature_survives_an_upgrade_without_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.dict(os.environ, {'SIGNATURE': '来自小明的AI助手'}):
                fixture.install()
            fixture.install()
            self.assertEqual(fixture.config()['signature'], '来自小明的AI助手')

    def test_new_signature_overrides_the_old_one(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.dict(os.environ, {'SIGNATURE': '旧署名'}):
                fixture.install()
            with patch.dict(os.environ, {'SIGNATURE': '新署名'}):
                fixture.install()
            self.assertEqual(fixture.config()['signature'], '新署名')

    def test_signature_is_optional_and_normalized(self):
        without_key = {}
        self.assertIsNone(install.resolve_signature(without_key, {}))
        self.assertIsNone(install.resolve_signature({'signature': '   '}, {}))
        self.assertEqual(install.resolve_signature({'signature': ' 来自  小明 '}, {}), '来自 小明')
        self.assertEqual(install.resolve_signature({'signature': '旧'}, {'SIGNATURE': '新'}), '新')
        with self.assertRaises(RuntimeError):
            install.resolve_signature({}, {'SIGNATURE': 'x' * (install.SIGNATURE_MAX + 1)})


class IdentityHasNoConfigSurfaceTests(unittest.TestCase):
    """身份是账号事实，不是配置项：安装器不许再长出第二个"人名来源"。

    **例外（2026-11-20，as_bot）**：`--owner-open-id` / `identity.owner_open_ids` 是
    **approver 名单**（机器人模式下"谁有权批准、谁有权发命令"），不是"这台机器是谁"——
    机器身份仍然只有 user / bot 两种，且一人一台机器只有一种。所以：
    * 仍然禁止"人名"性质的配置（owner_name / 探测本机用户名）；
    * 允许 owner 名单，但**只在 bot 模式下出现**：user 模式不写这一项（本人就是 target_open_id）。
    """

    def test_installer_has_no_owner_name_surface(self):
        source = (Path(__file__).resolve().parents[1] / 'install.py').read_text()
        for banned in ('owner_name', 'detect_owner_name', '--owner-name', 'OWNER_NAME'):
            self.assertNotIn(banned, source)

    def test_owner_list_is_bot_only(self):
        user_block = install.identity_config({'target_open_id': 'ou_owner'}, mode='user')
        mode, _ = install.identity_validate(user_block, target_open_id='ou_owner')
        self.assertEqual(mode, 'user')
        self.assertNotIn('owner_open_ids', user_block, 'user 模式的本人只有 target_open_id，不写名单')

        bot_block = install.identity_config({'target_open_id': 'ou_owner'}, mode='bot',
                                            bot_app_id='cli_demo', bot_app_secret_file='/tmp/x')
        mode, notes = install.identity_validate(bot_block, target_open_id='ou_owner')
        self.assertEqual(mode, 'bot')
        self.assertIn('未配置 identity.bot.bot_open_id', ' '.join(notes))


class SidecarDepsTests(unittest.TestCase):
    """sidecar 依赖必须由安装器准备好：没依赖时进程一启动就崩（"装了不启动"的老毛病）。"""

    def _fake_npm(self, root, *, exit_code=0, create_sdk=True):
        npm = Path(root) / 'npm'
        body = '#!/bin/sh\n'
        if create_sdk:
            body += 'mkdir -p "$PWD/node_modules/@larksuiteoapi/node-sdk"\n'
        body += 'exit %d\n' % exit_code
        npm.write_text(body)
        npm.chmod(0o755)
        return str(npm)

    def _home_with_sidecar(self, root):
        home = Path(root) / 'home'
        src = home / 'current' / 'sidecar'
        src.mkdir(parents=True, exist_ok=True)
        (src / 'index.js').write_text('console.log("hi")\n')
        (src / 'package.json').write_text('{"dependencies": {}}\n')
        return home

    def test_installs_deps_once_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as root:
            home = self._home_with_sidecar(root)
            npm = self._fake_npm(root)
            entry = install.ensure_sidecar_deps(home, 'node', npm_binary=npm)
            self.assertTrue(Path(entry).is_file())
            self.assertTrue((home / install.IDENTITY_SIDECAR_APP / 'node_modules' /
                             '@larksuiteoapi' / 'node-sdk').is_dir())
            stamp = Path(entry).stat().st_mtime_ns
            install.ensure_sidecar_deps(home, 'node', npm_binary=npm)  # 再跑一次：不该重装
            self.assertEqual(Path(entry).stat().st_mtime_ns, stamp)

    def test_failure_is_loud_with_next_step(self):
        with tempfile.TemporaryDirectory() as root:
            home = self._home_with_sidecar(root)
            npm = self._fake_npm(root, exit_code=1, create_sdk=False)
            with self.assertRaises(RuntimeError) as caught:
                install.ensure_sidecar_deps(home, 'node', npm_binary=npm)
            self.assertIn('sidecar 依赖安装失败', str(caught.exception))
            self.assertIn('npm install', str(caught.exception))

    def test_missing_npm_is_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as root:
            home = self._home_with_sidecar(root)
            with patch.object(install.shutil, 'which', return_value=None):
                with self.assertRaises(RuntimeError) as caught:
                    install.ensure_sidecar_deps(home, 'node')
            self.assertIn('npm', str(caught.exception))


class IdentityModeInstallTests(unittest.TestCase):
    """一台机器一种身份：as_bot 装 sidecar，as_user 必须把它清干净。"""

    def test_bot_mode_writes_sidecar_assets_and_unit_without_touching_real_services(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            secret = Path(root) / 'app-secret'
            secret.write_text('demo-secret\n')
            with patch.object(install, '_service_dir_is_real', return_value=False), \
                    patch.object(install, 'ensure_sidecar_deps',
                                 return_value=fixture.home / 'app' / 'index.js'), \
                    patch.object(install.host, 'reload_service') as reload_service:
                receipt = fixture.install(identity_mode='bot', bot_app_id='cli_demo',
                                          bot_app_secret_file=str(secret),
                                          owner_open_ids=['ou_owner'], activate=True)
            reload_service.assert_not_called()
            config = fixture.config()
            self.assertEqual(config['identity']['mode'], 'bot')
            self.assertEqual(config['identity']['bot']['sidecar'], 'service')
            env_path = fixture.home / install.IDENTITY_SIDECAR_ENV
            self.assertTrue(env_path.is_file())
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            self.assertIn('FEISHU_APP_ID=cli_demo', env_path.read_text())
            wrapper = fixture.home / 'bin' / install.IDENTITY_SIDECAR_BIN
            self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)
            release = fixture.release_path()
            self.assertTrue((release / 'sidecar' / 'index.js').is_file(), 'sidecar 必须随 release 走')
            self.assertEqual(receipt['identity']['mode'], 'bot')
            self.assertIn('skipped', receipt['identity']['sidecar'])

    def test_bot_mode_refuses_to_install_without_app_id(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with self.assertRaises(RuntimeError) as caught:
                fixture.install(identity_mode='bot', owner_open_ids=['ou_owner'])
            self.assertIn('--bot-app-id', str(caught.exception))
            self.assertFalse((fixture.home / 'config.json').exists(), '失败必须发生在写盘之前')

    def test_bot_mode_refuses_without_secret_source(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with self.assertRaises(RuntimeError) as caught:
                fixture.install(identity_mode='bot', bot_app_id='cli_demo',
                                owner_open_ids=['ou_owner'])
            self.assertIn('--bot-app-secret-file', str(caught.exception))

    def test_user_mode_retires_a_leftover_sidecar(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.home.mkdir(parents=True)
            (fixture.home / install.IDENTITY_SIDECAR_ENV).write_text('FEISHU_APP_ID=x\n')
            (fixture.home / 'bin').mkdir(parents=True, exist_ok=True)
            (fixture.home / 'bin' / install.IDENTITY_SIDECAR_BIN).write_text('#!/bin/sh\n')
            with patch.object(install, '_service_dir_is_real', return_value=False), \
                    patch.object(install.host, 'service_state', return_value={'loaded': False}):
                receipt = fixture.install(activate=True)
            self.assertEqual(receipt['identity']['mode'], 'user')
            self.assertFalse((fixture.home / install.IDENTITY_SIDECAR_ENV).exists(), 'user 模式不许留 sidecar')
            self.assertFalse((fixture.home / 'bin' / install.IDENTITY_SIDECAR_BIN).exists())
            self.assertNotIn('owner_open_ids', fixture.config()['identity'])

    def test_owner_list_survives_bot_install(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            secret = Path(root) / 'app-secret'
            secret.write_text('demo-secret\n')
            with patch.object(install, '_service_dir_is_real', return_value=False), \
                    patch.object(install, 'ensure_sidecar_deps',
                                 return_value=fixture.home / 'app' / 'index.js'):
                fixture.install(identity_mode='bot', bot_app_id='cli_demo',
                                bot_app_secret_file=str(secret), owner_open_ids=['ou_a', 'ou_b'],
                                activate=True)
            self.assertEqual(fixture.config()['identity']['owner_open_ids'], ['ou_a', 'ou_b'])


class InstallTests(unittest.TestCase):
    def test_installer_removes_the_retired_cursor_consumer_plist(self):
        """引擎 A 已删除：安装时必须清掉它的 LaunchAgent 和 park 副本，
        否则下次登录 launchd 会反复启动一个指向已删命令的进程。"""
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            retired = fixture.plist.with_name(install.RETIRED_CONSUMER_LABEL + '.plist')
            retired.parent.mkdir(parents=True, exist_ok=True)
            retired.write_bytes(plistlib.dumps({'Label': install.RETIRED_CONSUMER_LABEL,
                                                'ProgramArguments': ['/x/feishu-bridge',
                                                                     'consumer', 'foreground']}))
            parked_dir = fixture.home / install.PARKED_DIR
            parked_dir.mkdir(parents=True, exist_ok=True)
            parked = parked_dir / retired.name
            parked.write_bytes(retired.read_bytes())
            receipt = fixture.install()
            self.assertFalse(retired.exists(), '退役的 cursor LaunchAgent 没有被删除')
            self.assertFalse(parked.exists(), '退役的 cursor park 副本没有被删除')
            self.assertEqual(receipt['retired_consumer_removed'], [str(retired), str(parked)])
            self.assertNotIn('consumer_plist', receipt)

    def test_every_release_ships_the_consumer_runtime(self):
        """release 必须自带消费者运行时：Windows/WSL2 的 unit 指向 release，不再指向仓库树。"""
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            receipt = fixture.install()
            release = fixture.home / 'releases' / receipt['release_id']
            for name in install.CONSUMER_FILES:
                path = release / name
                self.assertTrue(path.is_file(), name)
                self.assertFalse(path.is_symlink(), name)
                self.assertEqual(receipt['consumer_files'][name],
                                 install.hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertTrue(os.access(str(release / 'dsh-consumer.py'), os.X_OK))

    def test_install_activates_release_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            receipt = fixture.install()
            release = fixture.release_path()
            self.assertEqual(receipt['release_id'], install._release_id(fixture.source))
            # receipt 的 files 覆盖真正会跑的全部文件：bridge/*.py + 根级消费者模块
            expected_files = install._file_hashes(install._bridge_files(fixture.source))
            for name in install.CONSUMER_FILES:
                expected_files[name] = install._file_hashes([fixture.source / name])[name]
            self.assertEqual(receipt['files'], expected_files)
            self.assertIsInstance(receipt['installed_at'], int)
            self.assertGreater(receipt['installed_at'], 0)
            self.assertEqual(receipt['source_sha256'], install._release_digest(fixture.source))
            current = fixture.home / 'current'
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.resolve(), release.resolve())
            self.assertEqual((current / 'bridge' / 'cli.py').read_text(), SOURCE_FILES['cli.py'])
            self.assertEqual(fixture.receipt(), receipt)
            launcher = fixture.home / 'bin' / 'feishu-bridge'
            self.assertTrue(launcher.is_file())
            self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
            self.assertTrue(fixture.link.is_symlink())
            self.assertEqual(fixture.link.resolve(), launcher.resolve())

    def test_launcher_makes_the_cli_return_value_the_process_exit_code(self):
        # 曾经启动器直接 `main(str(home))`：返回值被丢掉，`doctor --assert` 永远是 0，
        # 于是所有"退出码断言"的自动化都静默通过。判据：真跑启动器，看进程退出码。
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            package = home / 'current' / 'bridge'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('')
            (package / 'cli.py').write_text('def main(home):\n    return 3\n')
            launcher = home / 'bin' / 'feishu-bridge'
            launcher.parent.mkdir(parents=True)
            launcher.write_text(install.LAUNCHER_TEMPLATE % ('#!/bin/sh', str(home)))
            completed = subprocess.run([sys.executable, str(launcher)], capture_output=True, text=True,
                                       timeout=60)
        self.assertEqual(completed.returncode, 3, completed.stderr)

    @unittest.skipUnless(install.host.is_macos(), 'macOS launcher shebang')
    def test_macos_launcher_uses_the_installing_interpreter(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.install()
            first = (fixture.home / 'bin' / 'feishu-bridge').read_text().splitlines()[0]
        self.assertTrue(first.startswith('#!/'))
        self.assertIn('python', first)
        self.assertNotEqual(first, '#!/usr/bin/env python3')

    def test_install_plist_sets_exit_timeout_60_and_owns_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.install()
            plist = plistlib.loads(fixture.plist.read_bytes())
            self.assertEqual(plist['ExitTimeOut'], 60)
            self.assertEqual(plist['Label'], install.LABEL)
            self.assertEqual(plist['ProgramArguments'],
                             [str(fixture.home / 'bin' / 'feishu-bridge'), 'foreground'])
            self.assertEqual(plist['WorkingDirectory'], str(fixture.home))
            self.assertTrue(plist['KeepAlive'])

    def test_symlinked_release_copy_is_rebuilt_instead_of_trusted(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.install()
            release = fixture.release_path()
            bridge = release / 'bridge'
            shutil.rmtree(bridge)
            bridge.symlink_to(fixture.source / 'bridge', target_is_directory=True)
            fixture.install()
            self.assertFalse(bridge.is_symlink())
            self.assertTrue(all(not path.is_symlink() for path in bridge.glob('*.py')))

    def test_launchagent_pointing_at_a_prefix_lookalike_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.install()
            lookalike = fixture.root / 'home-evil' / 'bin' / 'feishu-bridge'
            plist = plistlib.loads(fixture.plist.read_bytes())
            plist['ProgramArguments'] = [str(lookalike), 'foreground']
            fixture.plist.write_bytes(plistlib.dumps(plist))
            (fixture.source / 'bridge' / 'cli.py').write_text('from . import service\n# v2\n')
            with self.assertRaises(RuntimeError) as caught:
                fixture.install()
        self.assertIn('not owned', str(caught.exception))

    def test_prune_releases_keeps_current_and_newest(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            releases = home / 'releases'
            releases.mkdir(parents=True)
            names = ['%016x' % index for index in range(1, 6)]
            for offset, name in enumerate(names):
                path = releases / name
                path.mkdir()
                os.utime(path, (offset + 1, offset + 1))
            with patch.dict(os.environ, {'FEISHU_RELEASE_KEEP': '2'}):
                removed = install._prune_releases(home, names[0])
            left = sorted(path.name for path in releases.iterdir())
        self.assertEqual(removed, sorted({names[1], names[2]}))
        self.assertEqual(left, sorted({names[0], names[3], names[4]}))

    def test_prune_releases_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            releases = home / 'releases'
            releases.mkdir(parents=True)
            for index in range(3):
                (releases / ('%016x' % index)).mkdir()
            with patch.dict(os.environ, {'FEISHU_RELEASE_KEEP': '0'}):
                removed = install._prune_releases(home, '%016x' % 0)
            self.assertEqual(removed, [])
            self.assertEqual(len(list(releases.iterdir())), 3)

    def test_release_identity_comes_from_the_shipped_module(self):
        from bridge import release as brelease
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            self.assertEqual(install._release_id(fixture.source), brelease.release_id(fixture.source))
            self.assertEqual(len(install._release_id(fixture.source)), 16)

    def test_reinstall_rebuilds_corrupt_release_copy(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.install()
            release = fixture.release_path()
            (release / 'bridge' / 'store.py').unlink()
            fixture.install()
            self.assertEqual((release / 'bridge' / 'store.py').read_text(), SOURCE_FILES['store.py'])

    def test_half_copied_release_is_never_promoted_to_current(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            release = fixture.release_path()
            (release / 'bridge').mkdir(parents=True)
            (release / 'bridge' / 'cli.py').write_text('# interrupted copy\n')
            fixture.install()
            copied = install._file_hashes(sorted((release / 'bridge').glob('*.py')))
            self.assertEqual(copied, install._file_hashes(install._bridge_files(fixture.source)))
            self.assertEqual((fixture.home / 'current' / 'bridge' / 'cli.py').read_text(),
                             SOURCE_FILES['cli.py'])

    def test_failed_release_copy_keeps_previous_release_active(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            first = fixture.install()
            (fixture.source / 'bridge' / 'cli.py').write_text('from . import service\n# v2\n')
            with patch.object(install.shutil, 'copytree', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    fixture.install()
            self.assertEqual((fixture.home / 'current').resolve(),
                             (fixture.home / 'releases' / first['release_id']).resolve())
            self.assertEqual(fixture.receipt(), first)

    def test_failed_launcher_write_does_not_switch_current(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            first = fixture.install()
            (fixture.source / 'bridge' / 'cli.py').write_text('from . import service\n# v2\n')
            original_write = install._atomic_write

            def fail_launcher(path, data, mode=0o600):
                if Path(path).name == 'feishu-bridge':
                    raise OSError('launcher write failed')
                return original_write(path, data, mode)

            with patch.object(install, '_atomic_write', side_effect=fail_launcher):
                with self.assertRaises(OSError):
                    fixture.install()
            self.assertEqual((fixture.home / 'current').resolve(),
                             (fixture.home / 'releases' / first['release_id']).resolve())
            self.assertEqual(fixture.receipt(), first)

    def test_upgrade_keeps_existing_release_directories(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            first = fixture.install()
            (fixture.source / 'bridge' / 'cli.py').write_text('from . import service\n# changed\n')
            second = fixture.install()
            self.assertNotEqual(first['release_id'], second['release_id'])
            self.assertTrue((fixture.home / 'releases' / first['release_id'] / 'bridge').is_dir())
            self.assertEqual((fixture.home / 'current').resolve(),
                             (fixture.home / 'releases' / second['release_id']).resolve())

    def test_install_lock_conflict_aborts_before_writes(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.object(install.fcntl, 'flock', side_effect=BlockingIOError('busy')):
                with self.assertRaises(RuntimeError) as caught:
                    fixture.install()
            self.assertIn('install.py', str(caught.exception))
            self.assertFalse((fixture.home / 'current').exists())
            self.assertFalse((fixture.home / 'installation.json').exists())

    def test_refuses_foreign_launcher_link_before_writes(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.link.parent.mkdir(parents=True)
            fixture.link.write_text('#!/bin/sh\n# someone else\n')
            with self.assertRaises(RuntimeError) as caught:
                fixture.install()
            self.assertIn('not owned', str(caught.exception))
            self.assertFalse((fixture.home / 'current').exists())

    def test_refuses_foreign_plist_before_writes(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.plist.parent.mkdir(parents=True)
            fixture.plist.write_bytes(plistlib.dumps({'Label': install.LABEL,
                                                      'ProgramArguments': ['/usr/local/bin/other']}))
            with self.assertRaises(RuntimeError) as caught:
                fixture.install()
            self.assertIn('not owned', str(caught.exception))
            self.assertFalse((fixture.home / 'current').exists())

    def test_refuses_corrupt_existing_config_before_writes(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.home.mkdir(parents=True)
            (fixture.home / 'config.json').write_text('{broken')
            with self.assertRaises(RuntimeError):
                fixture.install()
            self.assertFalse((fixture.home / 'current').exists())

    def test_reports_missing_runtime_binaries(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.lark.unlink()
            with self.assertRaises(RuntimeError) as caught:
                fixture.install()
            self.assertIn('lark-cli', str(caught.exception))
            # 只有 lark-cli 是硬要求了：内置 analyst 已删除，不再检查 cursor-agent。
            fixture.lark.write_text('#!/bin/sh\n')
            self.assertTrue(fixture.install()['release_id'])

    def test_source_without_bridge_files_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            (fixture.source / 'bridge' / 'cli.py').unlink()
            for name in list(SOURCE_FILES):
                path = fixture.source / 'bridge' / name
                if path.exists():
                    path.unlink()
            with self.assertRaises(RuntimeError) as caught:
                fixture.install()
            self.assertIn('no Python files', str(caught.exception))
            self.assertFalse((fixture.home / 'current').exists())




class LinuxInstallTests(unittest.TestCase):
    """WSL2/Linux is an independent path: systemd unit instead of launchd plists."""

    def test_linux_install_writes_systemd_unit(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            unit = fixture.root / 'systemd' / (install.LABEL + '.service')
            with patch.object(install.host, 'SYSTEM', 'linux'), \
                    patch.object(install, '_systemctl', return_value=None), \
                    patch.dict(os.environ, {'TARGET_OPEN_ID': 'ou_linux_test'}):
                receipt = install.install(source=fixture.source, home=fixture.home, service=unit,
                                          link=fixture.link, lark_binary=fixture.lark)
            text = unit.read_text()
            self.assertIn('ExecStart=%s foreground' % (fixture.home / 'bin/feishu-bridge'), text)
            self.assertIn('WorkingDirectory=%s' % fixture.home, text)
            self.assertIn('WantedBy=default.target', text)
            self.assertEqual(receipt['service_backend'], 'systemd')
            self.assertEqual(receipt['service_unit'], str(unit))
            self.assertNotIn('plist', receipt)
            self.assertEqual(fixture.config()['target_open_id'], 'ou_linux_test')
            launcher = (fixture.home / 'bin/feishu-bridge').read_text()
            self.assertTrue(launcher.startswith('#!/usr/bin/env python3'))

    def test_install_requires_explicit_open_id(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            unit = fixture.root / 'systemd' / (install.LABEL + '.service')
            with patch.object(install.host, 'SYSTEM', 'linux'), \
                    patch.object(install, '_systemctl', return_value=None), \
                    patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    install.install(source=fixture.source, home=fixture.home, service=unit,
                                    link=fixture.link, lark_binary=fixture.lark)
            self.assertIn('TARGET_OPEN_ID', str(caught.exception))
            self.assertFalse((fixture.home / 'installation.json').exists())

    def test_legacy_named_system_unit_is_recognised_by_its_execstart(self):
        # 旧安装的系统级 unit 名字里带人名拼音：只认新名字会得出"没有系统级 unit"，
        # 于是又写一份用户级 unit 并启动 —— 两个 bridge 抢同一个 home。
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            systemd_dir = fixture.root / 'system'
            systemd_dir.mkdir()
            legacy = systemd_dir / 'com.example.legacy.feishu-personal-agent.service'
            legacy.write_text('[Service]\nExecStart=%s foreground\n' % (fixture.home / 'bin/feishu-bridge'))
            (systemd_dir / 'unrelated.service').write_text('[Service]\nExecStart=/usr/bin/true\n')
            user_unit = fixture.root / 'user' / (install.LABEL + '.service')
            with patch.object(install.host, 'SYSTEM', 'linux'), \
                    patch.object(install, 'SYSTEM_UNIT_DIR', str(systemd_dir)), \
                    patch.object(install, '_systemctl', return_value=None) as systemctl, \
                    patch.dict(os.environ, {'TARGET_OPEN_ID': 'ou_linux_test'}):
                receipt = install.install(source=fixture.source, home=fixture.home, service=user_unit,
                                          link=fixture.link, lark_binary=fixture.lark)
            self.assertEqual(receipt['service_scope'], 'system')
            self.assertEqual(receipt['service_unit'], str(legacy))
            self.assertIn('service_name_warning', receipt)
            systemctl.assert_not_called()          # 不 enable/不 daemon-reload 用户级 unit
            self.assertFalse(user_unit.exists())

    def test_native_windows_install_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.object(install.host, 'SYSTEM', 'windows'):
                with self.assertRaises(SystemExit) as caught:
                    install.install(source=fixture.source, home=fixture.home, plist=fixture.plist,
                                    link=fixture.link, lark_binary=fixture.lark)
            self.assertIn('WSL2', str(caught.exception))


class TargetOpenIdTests(unittest.TestCase):
    """W13: open_id 必须显式给出；静默默认会让新机器以别人身份回复。"""

    def test_resolve_prefers_env_then_existing_config(self):
        self.assertEqual(install.resolve_target_open_id({'target_open_id': 'ou_config'},
                                                        {'TARGET_OPEN_ID': 'ou_env'}), 'ou_env')
        self.assertEqual(install.resolve_target_open_id({'target_open_id': 'ou_config'}, {}), 'ou_config')

    def test_resolve_strips_whitespace(self):
        self.assertEqual(install.resolve_target_open_id({}, {'TARGET_OPEN_ID': '  ou_me\n'}), 'ou_me')

    def test_resolve_without_any_source_raises_actionable_error(self):
        with self.assertRaises(RuntimeError) as caught:
            install.resolve_target_open_id({}, {})
        message = str(caught.exception)
        self.assertIn('TARGET_OPEN_ID', message)
        self.assertIn('auth status', message)

    def test_resolve_rejects_values_that_are_not_feishu_open_ids(self):
        with self.assertRaises(RuntimeError) as caught:
            install.resolve_target_open_id({}, {'TARGET_OPEN_ID': 'someone@example.com'})
        self.assertIn('ou_', str(caught.exception))

    def test_any_wellformed_open_id_from_config_is_accepted_verbatim(self):
        # 不再认识"某个具体的人"的历史默认值：config 里存的 open_id 只要格式对，就原样沿用。
        # （身份来自本机登录的飞书账号；代码里出现任何人的 open_id 都是硬编码。）
        resolved = install.resolve_target_open_id({'target_open_id': 'ou_example000000000000000000000000'}, {})
        self.assertEqual(resolved, 'ou_example000000000000000000000000')

    def test_macos_install_with_existing_config_needs_no_env(self):
        # 作者本人的升级路径：config 里已有自己的 open_id，不再传环境变量也能装。
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.home.mkdir(parents=True)
            (fixture.home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_owner'}))
            with patch.dict(os.environ, {}, clear=True):
                install.install(source=fixture.source, home=fixture.home, plist=fixture.plist,
                                link=fixture.link, lark_binary=fixture.lark)
            self.assertEqual(fixture.config()['target_open_id'], 'ou_owner')

    def test_macos_install_without_open_id_fails_before_writing_state(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    install.install(source=fixture.source, home=fixture.home, plist=fixture.plist,
                                    link=fixture.link, lark_binary=fixture.lark)
            self.assertIn('TARGET_OPEN_ID', str(caught.exception))
            self.assertFalse((fixture.home / 'current').exists())
            self.assertFalse((fixture.home / 'installation.json').exists())


class ActivationTests(unittest.TestCase):
    """安装 = 构建 + 写入 + 激活。这里钉住三件事：默认不碰真机、成功要落回执、失败要非 0。"""

    def test_install_does_not_touch_the_service_unless_asked(self):
        # R5（审计）：CI/测试调用 install() 时绝不能 bootout 本机生产 LaunchAgent。
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.object(install, '_activate') as activate:
                receipt = fixture.install()
            activate.assert_not_called()
            self.assertIsNone(receipt['activation']['bridge'])
            self.assertIsNone(receipt['activation']['dsh_consumer'])

    def test_activation_result_is_written_back_into_the_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fake = {'attempted': True, 'ok': True, 'backend': 'launchd', 'pid': 7,
                    'release': install._release_id(fixture.source)}
            with patch.object(install, '_activate', return_value=fake):
                receipt = fixture.install(activate=True)
            self.assertEqual(receipt['activation']['bridge'], fake)
            on_disk = json.loads((fixture.home / 'installation.json').read_text())
            self.assertEqual(on_disk['activation']['bridge'], fake)

    def test_activation_of_a_systemd_system_unit_is_reported_not_faked(self):
        # WSL2：系统级 unit 由 finalize-systemd.sh（D 阶段）激活，安装器不许假装已经跑新代码。
        with patch.object(install.host, 'service_state', return_value={'loaded': True, 'pid': 12}), \
                patch('bridge.cli.main', side_effect=AssertionError('不该在这个分支调 CLI restart')):
            info = install._activate(Path('/tmp/home'), 'abcdef0123456789', True, backend='systemd-system')
        self.assertFalse(info['attempted'])
        self.assertTrue(info['ok'])
        self.assertIn('finalize-systemd.sh', info['hint'])

    def test_activation_waits_for_the_heartbeat_of_the_new_release(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            release = 'abcdef0123456789'
            (home / 'status.json').write_text(json.dumps(
                {'running_release': release, 'heartbeat_at': int(time.time()), 'pid': 4242}))

            def fake_restart(home_arg):
                return None

            with patch('bridge.cli.main', side_effect=fake_restart), \
                    patch.object(install, '_read_config', side_effect=lambda path: json.loads(
                        (home / 'status.json').read_text())):
                info = install._activate(home, release, True, wait_seconds=0, backend='launchd')
            self.assertTrue(info['ok'])
            self.assertEqual(info['pid'], 4242)

    def test_activation_failure_is_reported_with_the_log_path(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / 'status.json').write_text(json.dumps(
                {'running_release': 'oldrelease', 'heartbeat_at': int(time.time()) - 600}))
            with patch('bridge.cli.main', side_effect=SystemExit(2)), \
                    patch.object(install, '_read_config', side_effect=lambda path: json.loads(
                        (home / 'status.json').read_text())), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                info = install._activate(home, 'newrelease', True, wait_seconds=0,
                                         backend='launchd')
            self.assertFalse(info['ok'])
            self.assertIn('restart 退出码 2', info['restart_error'])
            self.assertIn('launchd.stderr.log', errors.getvalue())

    def test_main_exits_three_when_the_release_does_not_run(self):
        # 3 = 代码已提交但没跑起来（与 Windows 安装器「3 systemd 未就绪」同一含义）。
        receipt = {'home': '/tmp/home', 'activation': {'bridge': {'ok': False}, 'dsh_consumer': None}}
        with patch.object(install, 'install', return_value=receipt), \
                patch.object(sys, 'argv', ['install.py']), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                install.main()
        self.assertEqual(caught.exception.code, 3)


class VerifyRetryTests(unittest.TestCase):
    """刚重启时 doctor 可能看到 pid 与心跳没对齐：verify 允许 5 秒后重试一次，但真问题仍要 fail。"""

    def run_verify(self, codes):
        calls = []

        def fake_doctor(home, path, argv):
            calls.append(argv)
            return codes[min(len(calls) - 1, len(codes) - 1)]

        with tempfile.TemporaryDirectory() as root, \
                patch('bridge.cli._doctor', side_effect=fake_doctor), \
                patch.object(install, '_read_config', return_value={}), \
                patch.object(install.time, 'sleep'), \
                contextlib.redirect_stderr(io.StringIO()):
            code = install.verify_installation(Path(root), wait_seconds=0, restart=False)
        return code, calls

    def test_transient_failure_is_retried_once(self):
        code, calls = self.run_verify([1, 0])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)

    def test_a_real_failure_still_fails_after_the_retry(self):
        code, calls = self.run_verify([1, 1])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)

    def test_success_does_not_run_doctor_twice(self):
        code, calls = self.run_verify([0])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)


class DshConsumerInstallTests(unittest.TestCase):
    """macOS 的常驻消费者：与 Windows 的 install-dsh-consumer.sh 同一套事实与语义。"""

    def make_dsh(self, root):
        binary = Path(root) / 'bin' / 'dsh'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\necho 0.2.1-rc.5\n')
        binary.chmod(0o755)
        return binary

    def test_definition_matches_the_windows_unit(self):
        definition = install._dsh_consumer_definition(
            Path('/tmp/home'), 'dsh-main', 'dsh', Path('/tmp/ws'), '/opt/bin/dsh')
        self.assertEqual(definition['Label'], install.DSH_CONSUMER_LABEL)
        self.assertEqual(definition['Label'], 'io.feishu-agent.dsh-consumer')
        argv = definition['ProgramArguments']
        self.assertEqual(argv[0], '/tmp/home/current/dsh-consumer.py')
        self.assertEqual(argv[1:], ['--consumer', 'dsh-main', '--kind', 'dsh',
                                    '--home', '/tmp/home', '--workspace', '/tmp/ws'])
        self.assertTrue(definition['RunAtLoad'])
        self.assertTrue(definition['KeepAlive'])
        environment = definition['EnvironmentVariables']
        self.assertEqual(environment['PATH'].split(':')[0], '/opt/bin')
        self.assertTrue(environment['DSH_HOME'].endswith('.dsh'))
        self.assertEqual(definition['StandardOutPath'], '/tmp/home/dsh-consumer.stdout.log')

    def test_binary_resolution_prefers_the_explicit_override(self):
        with tempfile.TemporaryDirectory() as root:
            binary = self.make_dsh(root)
            with patch.dict(os.environ, {'DSH_BINARY': str(binary)}, clear=False):
                self.assertEqual(install._dsh_binary(), binary)
                self.assertEqual(install._dsh_version(binary), '0.2.1-rc.5')

    def test_missing_engine_fails_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(install.shutil, 'which', return_value=None), \
                patch.object(install.Path, 'home', return_value=Path(root)):
            with self.assertRaises(SystemExit) as caught:
                install._dsh_binary()
        self.assertIn('npm install -g @deepseek-ai/dsh', str(caught.exception))

    def test_linux_refuses_and_points_at_the_system_level_script(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            with patch.object(install.host, 'is_macos', return_value=False), \
                    patch.object(install.host, 'is_linux', return_value=True):
                with self.assertRaises(RuntimeError) as caught:
                    fixture.install(with_dsh_consumer=True)
            self.assertIn('install-dsh-consumer.sh', str(caught.exception))
            self.assertFalse((fixture.home / 'current').exists())

    def test_with_dsh_consumer_writes_a_private_plist_and_reports_liveness(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            binary = self.make_dsh(root)
            live = {'label': install.DSH_CONSUMER_LABEL, 'live': True, 'reloaded': True, 'pid': 9}
            with patch.object(install, '_reload_dsh_consumer', return_value=live) as reload:
                receipt = fixture.install(with_dsh_consumer=True, dsh_workspace=Path(root) / 'ws')
            plist = fixture.plist.parent / (install.DSH_CONSUMER_LABEL + '.plist')
            payload = plistlib.loads(plist.read_bytes())
            self.assertEqual(payload['Label'], install.DSH_CONSUMER_LABEL)
            self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
            self.assertTrue((Path(root) / 'ws').is_dir())
            self.assertTrue(callable(reload))
            self.assertEqual(receipt['activation']['dsh_consumer'], live)

    def test_reload_reports_not_live_when_the_consumer_never_claims(self):
        with patch.object(install, '_service_dir_is_real', return_value=True):
         with tempfile.TemporaryDirectory() as root:
            with patch.object(install.host, 'service_state', return_value={'loaded': True, 'pid': 1}), \
                    patch.object(install.host, 'reload_service',
                                 return_value={'ok': True, 'pid': 2, 'attempts': 1, 'error': None}), \
                    patch.object(install, '_wait_consumer_live', return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                info = install._reload_dsh_consumer(Path(root), Path('/tmp/p.plist'), 'dsh-main',
                                                    'dsh', Path('/tmp/ws'), '/opt/bin/dsh', '0.2.1')
            self.assertFalse(info['live'])
            self.assertIn('consumer status', errors.getvalue())

    def test_plain_upgrade_reloads_an_already_installed_consumer(self):
        # 只升 bridge、没带 --with-dsh-consumer：已装的消费者也必须重载到新 release，
        # 否则回复链路继续跑旧代码（"升级成功"只对一半进程成立）。
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.plist.parent.mkdir(parents=True, exist_ok=True)
            payload = {'Label': install.DSH_CONSUMER_LABEL,
                       'ProgramArguments': ['/home/current/dsh-consumer.py', '--consumer', 'dsh-main']}
            (fixture.plist.parent / (install.DSH_CONSUMER_LABEL + '.plist')).write_bytes(
                plistlib.dumps(payload))
            live = {'consumer': 'dsh-main', 'created': False, 'reloaded': True, 'live': True, 'pid': 11}
            with patch.object(install, '_activate', return_value={'ok': True}), \
                    patch.object(install, '_reload_existing_dsh_consumer', return_value=live) as reload:
                receipt = fixture.install(activate=True)
            reload.assert_called_once()
            self.assertEqual(receipt['activation']['dsh_consumer'], live)

    def test_existing_consumer_identity_is_read_from_the_unit_arguments(self):
        with tempfile.TemporaryDirectory() as root:
            units = Path(root)
            payload = {'Label': install.DSH_CONSUMER_LABEL,
                       'ProgramArguments': ['/home/current/dsh-consumer.py', '--consumer', 'dsh-2',
                                            '--kind', 'dsh']}
            (units / (install.DSH_CONSUMER_LABEL + '.plist')).write_bytes(plistlib.dumps(payload))
            existing = install._existing_dsh_consumer(units)
            self.assertEqual(existing['consumer'], 'dsh-2')
            self.assertIsNone(install._existing_dsh_consumer(units / 'missing'))

    def test_worker_unit_is_private_and_has_no_repo_or_worktree_arguments(self):
        """干活端单元只带 consumer/home：**没有** repo / 工作树——同事机上没有 git。"""
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            self.make_dsh(root)
            with patch.object(install, '_reload_dsh_consumer', return_value={'live': True}), \
                    patch.object(install, '_reload_dsh_worker', return_value={'live': True}) as reload:
                receipt = fixture.install(with_dsh_consumer=True, dsh_workspace=Path(root) / 'ws')
            plist = fixture.plist.parent / (install.DSH_WORKER_LABEL + '.plist')
            payload = plistlib.loads(plist.read_bytes())
            self.assertEqual(payload['Label'], install.DSH_WORKER_LABEL)
            self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
            argv = payload['ProgramArguments']
            self.assertNotIn('--repo', argv)
            self.assertNotIn('--worktree-root', argv)
            self.assertIn('--consumer', argv)
            self.assertIn('--home', argv)
            self.assertTrue(str(argv[0]).endswith('dsh-worker.py'))
            reload.assert_called_once()
            self.assertEqual(receipt['activation']['dsh_worker'], {'live': True})

    def test_plain_upgrade_reloads_an_already_installed_worker(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.plist.parent.mkdir(parents=True, exist_ok=True)
            payload = {'Label': install.DSH_WORKER_LABEL,
                       'ProgramArguments': ['/home/current/dsh-worker.py', '--consumer', 'w-1',
                                            '--repo', '/home/repo', '--worktree-root', '/home/wts']}
            (fixture.plist.parent / (install.DSH_WORKER_LABEL + '.plist')).write_bytes(
                plistlib.dumps(payload))
            live = {'consumer': 'w-1', 'reloaded': True, 'live': True}
            with patch.object(install, '_activate', return_value={'ok': True}), \
                    patch.object(install, '_reload_existing_dsh_consumer', return_value=None), \
                    patch.object(install, '_reload_dsh_worker', return_value=live) as reload, \
                    patch.object(install, '_service_dir_is_real', return_value=True), \
                    patch.object(install, '_dsh_binary_or_none', return_value='/opt/bin/dsh'):
                receipt = fixture.install(activate=True)
            reload.assert_called_once()
            self.assertEqual(reload.call_args[0][2], 'w-1', 'identity 要从单元参数里读，不靠猜')
            self.assertEqual(receipt['activation']['dsh_worker'], live)

    def test_release_entry_scripts_are_executable(self):
        """入口脚本必须 0700：launchd 直接 exec 它，0600 会得到 EX_CONFIG 78（日志还是空的）。"""
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            self.make_dsh(root)
            with patch.object(install, '_reload_dsh_consumer', return_value={'live': True}), \
                    patch.object(install, '_reload_dsh_worker', return_value={'live': True}):
                fixture.install(with_dsh_consumer=True, dsh_workspace=Path(root) / 'ws')
            release = Path(fixture.home) / 'releases' / fixture.release_id() \
                if hasattr(fixture, 'release_id') else None
            current = Path(fixture.home) / 'current'
            for name in ('dsh-consumer.py', 'dsh-worker.py'):
                path = current / name
                self.assertTrue(path.is_file(), name)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o111, 0o100,
                                 '%s 必须可执行，否则 launchd 直接 exec 会 EX_CONFIG' % name)

    def test_upgrade_writes_the_worker_unit_when_it_was_never_installed(self):
        """S3a 之前装的机器没有干活端单元：升级必须补写，而且写的东西要能真的跑。

        事故背景（2026-11-19 部署现场）：`install.py --verify` 走升级路径时只重载、不补写，
        `launchctl` 里连单元都没有，界面上却一切正常——"批准之后没人干活"看起来像活卡住了。
        """
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            fixture.plist.parent.mkdir(parents=True, exist_ok=True)
            live = {'consumer': install.DSH_WORKER_DEFAULT, 'reloaded': True, 'live': True}
            with patch.object(install, '_activate', return_value={'ok': True}), \
                    patch.object(install, '_reload_existing_dsh_consumer', return_value=None), \
                    patch.object(install, '_reload_dsh_worker', return_value=live) as reload, \
                    patch.object(install, '_service_dir_is_real', return_value=True), \
                    patch.object(install, '_dsh_binary_or_none', return_value='/opt/bin/dsh'):
                receipt = fixture.install(activate=True)
            plist = fixture.plist.parent / (install.DSH_WORKER_LABEL + '.plist')
            self.assertTrue(plist.is_file(), '升级路径必须把缺的干活端单元补上')
            self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
            argv = plistlib.loads(plist.read_bytes())['ProgramArguments']
            self.assertNotIn('--repo', argv, '干活端不假设机器上有 git，也就不需要主仓库参数')
            self.assertNotIn('--worktree-root', argv)
            reload.assert_called_once()
            self.assertEqual(receipt['activation']['dsh_worker'], live)

    def test_legacy_worker_unit_with_repo_args_is_rewritten_not_reloaded_as_is(self):
        """旧单元带着已删掉的 --repo/--worktree-root：新 dsh-worker.py 会拒收这些参数。

        所以升级路径必须**重写**单元，而不是"原样重载"——否则升级完干活端直接起不来。
        """
        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            units = fixture.plist.parent
            units.mkdir(parents=True, exist_ok=True)
            legacy = {'Label': install.DSH_WORKER_LABEL,
                      'ProgramArguments': ['/home/current/dsh-worker.py', '--consumer', 'w-1',
                                           '--repo', '/home/repo', '--worktree-root', '/home/wts']}
            (units / (install.DSH_WORKER_LABEL + '.plist')).write_bytes(plistlib.dumps(legacy))
            with patch.object(install, '_service_dir_is_real', return_value=True), \
                    patch.object(install, '_reload_dsh_worker', return_value={'live': True}) as reload, \
                    patch.object(install, '_dsh_binary_or_none', return_value='/opt/bin/dsh'):
                install._reload_existing_dsh_worker(fixture.home, units)
            argv = plistlib.loads((units / (install.DSH_WORKER_LABEL + '.plist')).read_bytes())['ProgramArguments']
            self.assertNotIn('--repo', argv)
            self.assertNotIn('--worktree-root', argv)
            self.assertIn('w-1', argv, '消费者身份要保留')
            reload.assert_called_once()

    def test_no_worker_plist_means_nothing_to_reload(self):
        self.assertIsNone(install._reload_existing_dsh_worker(Path('/tmp'), Path('/tmp')))

    def test_legacy_plist_is_removed_by_argv_even_without_feishu_in_the_label(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            units = Path(root) / 'LaunchAgents'
            units.mkdir(parents=True)
            home.mkdir(parents=True)
            owned = {'Label': 'com.someone.dsh-consumer',
                     'ProgramArguments': [str(home / 'current' / 'dsh-consumer.py')]}
            foreign = {'Label': 'com.example.other', 'ProgramArguments': ['/usr/bin/true']}
            (units / 'com.someone.dsh-consumer.plist').write_bytes(plistlib.dumps(owned))
            (units / 'com.example.other.plist').write_bytes(plistlib.dumps(foreign))
            removed = install._remove_legacy_services(home, units)
            self.assertIn(str(units / 'com.someone.dsh-consumer.plist'), removed)
            self.assertTrue((units / 'com.example.other.plist').exists())


if __name__ == '__main__':
    unittest.main()


class ConfigDefaultAgreementTests(unittest.TestCase):
    """安装默认值和服务端 fallback 是同一事实的两处写法，必须一致。

    为什么专门测：`live_interval_seconds` 的 12 曾经只写在 service 的 fallback 里，install 又抄了一遍；
    调采集节奏时只改了配置文件，两个代码里的数还留在原地，改动的效果看不出来也不知道为什么。
    """

    def test_scan_intervals_in_install_match_the_service_fallbacks(self):
        import inspect
        from bridge import service
        source = inspect.getsource(service.Service.collector)
        for key in ('live_interval_seconds', 'self_scan_seconds'):
            with self.subTest(key=key):
                self.assertIn('%r, %s' % (key, install.CONFIG_DEFAULTS[key]), source,
                              'install 默认值与 service fallback 不一致：%s' % key)

    def test_the_search_interval_default_is_documented_in_one_place(self):
        from bridge.transport import SEARCH_MIN_INTERVAL_SECONDS
        self.assertEqual(install.CONFIG_DEFAULTS['search_min_interval_seconds'],
                         SEARCH_MIN_INTERVAL_SECONDS)
