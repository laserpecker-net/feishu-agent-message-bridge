"""旧命名 unit / LaunchAgent 的迁移判据。

背景：unit 名曾是 `com.<作者拼音>.feishu-*`。改名后如果**不清理旧 unit**，旧 bridge 还在跑、
还 enabled，就会和新 unit 抢同一个 home（双采集、重复回复），而且旧名字里带着上一个人的姓名拼音。
所以"改名"必须带着"迁移"，且迁移的判据不能是"列一张旧名字清单"——那等于把姓名拼音写回仓库。
"""

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
MIGRATE = ROOT / 'dist/wsl2/wsl/migrate-legacy-units.sh'


def _write(path, text=''):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _run(*args):
    return subprocess.run(['bash', str(MIGRATE)] + list(args),
                          capture_output=True, text=True, timeout=60)


class MigrateLegacyUnitsTests(unittest.TestCase):
    def test_removes_legacy_units_and_keeps_new_ones(self):
        d = Path(tempfile.mkdtemp())
        legacy = d / 'com.someone.feishu-bridge.service'
        new = d / 'io.feishu-agent.bridge.service'
        unrelated = d / 'nginx.service'
        legacy_timer = d / 'com.someone.feishu-prune.timer'
        for f in (legacy, new, unrelated, legacy_timer):
            _write(f)
        result = _run('--dir', str(d))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(legacy.exists(), '旧命名的 unit 必须删掉')
        self.assertFalse(legacy_timer.exists(), '旧命名的 timer 同样要删')
        self.assertTrue(new.exists(), '新命名 unit 不能误删')
        self.assertTrue(unrelated.exists(), '无关 unit 不能碰')

    def test_cleans_wants_symlinks_of_legacy_units(self):
        d = Path(tempfile.mkdtemp())
        wants = d / 'io.feishu-agent.bridge.service.wants'
        (wants).mkdir(parents=True)
        _write(wants / 'com.someone.feishu-dsh-consumer.service')
        _write(wants / 'io.feishu-agent.dsh-consumer.service')
        result = _run('--dir', str(d))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((wants / 'com.someone.feishu-dsh-consumer.service').exists())
        self.assertTrue((wants / 'io.feishu-agent.dsh-consumer.service').exists())

    def test_check_mode_reports_without_deleting(self):
        d = Path(tempfile.mkdtemp())
        legacy = d / 'com.someone.feishu-bridge.service'
        _write(legacy)
        result = _run('--check', '--dir', str(d))
        self.assertEqual(result.returncode, 3, '有遗留时 --check 必须退出 3（verify.sh 的断言靠它）')
        self.assertIn('legacy_count=1', result.stdout)
        self.assertTrue(legacy.exists(), '--check 不许改系统')
        clean = _run('--check', '--dir', str(Path(tempfile.mkdtemp())))
        self.assertEqual(clean.returncode, 0)
        self.assertIn('legacy_count=0', clean.stdout)

    def test_only_units_with_feishu_in_the_name_are_touched(self):
        d = Path(tempfile.mkdtemp())
        keep = d / 'com.someone.other-app.service'
        _write(keep)
        _run('--dir', str(d))
        self.assertTrue(keep.exists(), '名字里没有 feishu 的 unit 一律不动')


class WiringTests(unittest.TestCase):
    """迁移必须真的被安装/验收路径调用，否则脚本存在也没用。"""

    def test_finalize_calls_migration(self):
        text = (ROOT / 'dist/wsl2/wsl/finalize-systemd.sh').read_text()
        self.assertIn('migrate-legacy-units.sh', text)

    def test_consumer_install_calls_migration(self):
        text = (ROOT / 'dist/wsl2/wsl/install-dsh-consumer.sh').read_text()
        self.assertIn('migrate-legacy-units.sh', text)

    def test_verify_asserts_no_legacy_units(self):
        text = (ROOT / 'dist/wsl2/wsl/verify.sh').read_text()
        self.assertIn('legacy_units', text)
        self.assertIn('--check', text)

    def test_install_py_removes_legacy_launch_agents(self):
        text = (ROOT / 'install.py').read_text()
        self.assertIn('_remove_legacy_services', text)
        # 判据不能是"旧名字清单"：那只会在仓库里重新写出上一个人的姓名拼音
        self.assertNotIn('liu' + 'haibao', text)


class MacLaunchAgentMigrationTests(unittest.TestCase):
    """install.py 侧的同一件事：旧 label 的 plist（含 parked 副本）必须被删，当前 label 不能碰。"""

    def _write_plist(self, path, label):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({'Label': label}))

    def test_removes_legacy_labels_and_keeps_current_ones(self):
        import install
        home = Path(tempfile.mkdtemp())
        agents = home / 'Library' / 'LaunchAgents'
        legacy = agents / 'com.someone.feishu-bridge.plist'
        current = agents / 'io.feishu-agent.bridge.plist'
        unrelated = agents / 'com.other.app.plist'
        parked = home / install.PARKED_DIR / 'com.someone.feishu-dsh-consumer.plist'
        self._write_plist(legacy, 'com.someone.feishu-bridge')
        self._write_plist(current, install.LABEL)
        self._write_plist(unrelated, 'com.other.app')
        self._write_plist(parked, 'com.someone.feishu-dsh-consumer')
        with mock.patch.object(install.host, 'is_macos', return_value=False):
            removed = install._remove_legacy_services(str(home), agents)
        self.assertEqual(2, len(removed), removed)
        self.assertFalse(legacy.exists())
        self.assertFalse(parked.exists())
        self.assertTrue(current.exists(), '当前 label 的 plist 必须保留')
        self.assertTrue(unrelated.exists(), '别人的 plist 一律不动')

    def test_keeps_attention_watch_label(self):
        import install
        home = Path(tempfile.mkdtemp())
        agents = home / 'Library' / 'LaunchAgents'
        attention = agents / 'io.feishu-agent.attention-watch.plist'
        self._write_plist(attention, install.ATTENTION_LABEL)
        with mock.patch.object(install.host, 'is_macos', return_value=False):
            removed = install._remove_legacy_services(str(home), agents)
        self.assertEqual([], removed)
        self.assertTrue(attention.exists())


if __name__ == '__main__':
    unittest.main()
