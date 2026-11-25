"""as_bot 交付缺口的两处判据：安装器自动备好 ingest 通道、体检按模式下判据。

背景（2026-11-21 真机）：as_bot 在 Windows 上跑通，但下面两件事是**手工补的**——
换台机器照文档装就会踩：

1. sidecar 启动即读 ``<home>/gateway-token``，并把事件 ``POST 127.0.0.1:8787/v1/ingest``；
   安装器当时既不生成 token，也不起回环网关（且 ``events serve`` 默认 8766，与 sidecar 不一致）。
2. ``doctor`` 的 ``authenticated`` 判据按"用户 token"写，as_bot 下永久误报 fail。

判据只认"可复跑"：装完能读到 token、网关 /health 通、doctor 五步全 ok。
"""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import install
from bridge import gateway as gateway_module
from bridge.identity import factory as identity_factory


def _temp_service_dir(root):
    path = Path(root) / 'LaunchAgents'
    path.mkdir(parents=True, exist_ok=True)
    return path


class IngestGatewayInstallTests(unittest.TestCase):
    def _install(self, root, port=None):
        home = Path(root) / 'home'
        home.mkdir(parents=True, exist_ok=True)
        block = {'mode': 'bot', 'bot': {'app_id': 'cli_demo'}}
        if port is not None:
            block['bot']['gateway_port'] = port
        service_dir = _temp_service_dir(root)
        with patch.object(install.host, 'service_dir', return_value=service_dir), \
                patch.object(install, '_service_dir_is_real', return_value=True), \
                patch.object(install.host, 'reload_service',
                             return_value={'ok': True, 'pid': 4321}), \
                patch.object(gateway_module, 'health', return_value=(True, 'ok 200')):
            info = install.install_ingest_gateway(home, block)
        return home, service_dir, info

    def test_writes_private_token_and_unit_then_reports_health(self):
        with tempfile.TemporaryDirectory() as root:
            home, service_dir, info = self._install(root)
            token_path = gateway_module.token_path(home)
            self.assertTrue(token_path.is_file(), '安装器必须生成共享 token')
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            self.assertEqual(info['port'], 8787)
            self.assertTrue(info['reloaded'])
            self.assertTrue(info['health'])
            unit = service_dir / (install.INGEST_GATEWAY_LABEL + '.plist')
            if not unit.exists():
                unit = service_dir / (install.INGEST_GATEWAY_LABEL + '.service')
            self.assertTrue(unit.is_file(), '必须写网关单元')
            text = unit.read_text(encoding='utf-8', errors='replace')
            self.assertIn('events', text)
            self.assertIn('serve', text)
            self.assertIn('8787', text)

    def test_existing_token_is_reused_not_rotated(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir(parents=True)
            first, _, created = gateway_module.generate_token(home)
            self.assertTrue(created)
            _, _, info = self._install(root)
            self.assertFalse(info['token_created'], '重复安装不许轮换 token（会让已装 sidecar 失联）')
            self.assertEqual(gateway_module.token_path(home).read_text().strip(), first)

    def test_unhealthy_gateway_is_reported_not_swallowed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir(parents=True)
            service_dir = _temp_service_dir(root)
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=True), \
                    patch.object(install.host, 'reload_service', return_value={'ok': True}), \
                    patch.object(gateway_module, 'health', return_value=(False, 'conn refused')):
                info = install.install_ingest_gateway(home, {'mode': 'bot', 'bot': {'app_id': 'x'}})
            self.assertFalse(info['health'])
            self.assertIn('conn refused', info['health_detail'])

    def test_temporary_service_dir_only_writes_files(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir(parents=True)
            service_dir = _temp_service_dir(root)
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=False), \
                    patch.object(install.host, 'reload_service') as reload_service:
                info = install.install_ingest_gateway(home, {'mode': 'bot', 'bot': {'app_id': 'x'}})
            reload_service.assert_not_called()
            self.assertIn('skipped', info)
            self.assertTrue(gateway_module.token_path(home).is_file())

    def test_retire_stops_and_removes_the_unit(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir(parents=True)
            service_dir = _temp_service_dir(root)
            unit = service_dir / (install.INGEST_GATEWAY_LABEL + '.plist')
            unit.write_text('x')
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=True), \
                    patch.object(install.host, 'service_state', return_value={'loaded': False}):
                result = install.retire_ingest_gateway(home)
            self.assertFalse(unit.exists(), 'user 模式必须清掉网关单元')
            self.assertIn(str(unit), result['removed'])

    def test_retire_is_idempotent_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir(parents=True)
            service_dir = _temp_service_dir(root)
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=False):
                self.assertIsNone(install.retire_ingest_gateway(home))

    def test_bad_port_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir(parents=True)
            service_dir = _temp_service_dir(root)
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=False):
                with self.assertRaises(RuntimeError) as caught:
                    install.install_ingest_gateway(
                        home, {'mode': 'bot', 'bot': {'app_id': 'x', 'gateway_port': 'abc'}})
            self.assertIn('gateway_port', str(caught.exception))


class BotInstallWiresGatewayTests(unittest.TestCase):
    """装 as_bot 时，安装器要把端口写进配置、并按顺序先备 token 再起 sidecar。"""

    def test_bot_install_sets_gateway_port_and_creates_token(self):
        from tests.test_install import InstallFixture

        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            secret = Path(root) / 'app-secret'
            secret.write_text('demo-secret\n')
            service_dir = _temp_service_dir(root)
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=False), \
                    patch.object(install, '_activate', return_value={'ok': True}), \
                    patch.object(install, 'ensure_sidecar_deps',
                                 return_value=fixture.home / 'app' / 'index.js'):
                receipt = fixture.install(identity_mode='bot', bot_app_id='cli_demo',
                                          bot_app_secret_file=str(secret),
                                          owner_open_ids=['ou_owner'], activate=True)
            config = fixture.config()
            self.assertEqual(config['identity']['bot']['gateway_port'], 8787)
            self.assertTrue(gateway_module.token_path(fixture.home).is_file())
            self.assertEqual(receipt['identity']['ingest_gateway']['port'], 8787)
            self.assertEqual(stat.S_IMODE(gateway_module.token_path(fixture.home).stat().st_mode), 0o600)

    def test_user_install_leaves_no_ingest_gateway(self):
        from tests.test_install import InstallFixture

        with tempfile.TemporaryDirectory() as root:
            fixture = InstallFixture(root)
            service_dir = _temp_service_dir(root)
            unit = service_dir / (install.INGEST_GATEWAY_LABEL + '.plist')
            unit.write_text('x')
            with patch.object(install.host, 'service_dir', return_value=service_dir), \
                    patch.object(install, '_service_dir_is_real', return_value=False), \
                    patch.object(install, '_activate', return_value={'ok': True}), \
                    patch.object(install.host, 'service_state', return_value={'loaded': False}):
                receipt = fixture.install(activate=True)
            self.assertFalse(unit.exists(), 'user 模式不许留 ingest 网关单元')
            retired = receipt['identity']['ingest_gateway']
            self.assertTrue(retired is None or retired.get('removed'),
                            'user 模式下网关必须被清掉（返回 removed 或 None）')


class DoctorIdentityChecksTests(unittest.TestCase):
    """体检项由身份层给：as_user 判用户 token；as_bot 判 bot 身份 + token + 回环网关。"""

    def _checks(self, config, home, identity):
        return {item['name']: item for item in identity_factory.doctor_checks(config, home, identity)}

    def test_user_mode_still_judges_the_user_token(self):
        config = {'identity': {'mode': 'user'}}
        checks = self._checks(config, None, {'tokenStatus': 'valid', 'available': True})
        self.assertEqual(checks['authenticated']['status'], 'ok')
        self.assertNotIn('ingest_token', checks)
        bad = self._checks(config, None, {'tokenStatus': 'invalid', 'available': False})
        self.assertEqual(bad['authenticated']['status'], 'fail')

    def test_bot_mode_passes_when_identity_token_and_gateway_are_all_ready(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            gateway_module.generate_token(home)
            config = {'identity': {'mode': 'bot', 'bot': {'app_id': 'cli_demo', 'gateway_port': 8787}}}
            with patch.object(gateway_module, 'health', return_value=(True, 'ok')):
                checks = self._checks(config, home, {'appId': 'cli_demo', 'available': True,
                                                     'tokenStatus': 'valid'})
            self.assertEqual(checks['authenticated']['status'], 'ok')
            self.assertEqual(checks['ingest_token']['status'], 'ok')
            self.assertEqual(checks['ingest_gateway']['status'], 'ok')

    def test_bot_mode_flags_a_missing_token_and_dead_gateway(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            config = {'identity': {'mode': 'bot', 'bot': {'app_id': 'cli_demo'}}}
            with patch.object(gateway_module, 'health', return_value=(False, 'conn refused')):
                checks = self._checks(config, home, {'appId': 'cli_demo', 'available': True,
                                                     'tokenStatus': 'valid'})
            self.assertEqual(checks['ingest_token']['status'], 'fail')
            self.assertIn('install.py', checks['ingest_token']['detail'])
            self.assertEqual(checks['ingest_gateway']['status'], 'fail')
            self.assertIn('conn refused', checks['ingest_gateway']['detail'])

    def test_bot_mode_flags_app_id_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            gateway_module.generate_token(home)
            config = {'identity': {'mode': 'bot', 'bot': {'app_id': 'cli_config'}}}
            with patch.object(gateway_module, 'health', return_value=(True, 'ok')):
                checks = self._checks(config, home, {'appId': 'cli_other', 'available': True,
                                                     'tokenStatus': 'valid'})
            self.assertEqual(checks['authenticated']['status'], 'fail')
            self.assertIn('不一致', checks['authenticated']['detail'])

    def test_bot_mode_flags_unauthenticated_bot(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            gateway_module.generate_token(home)
            config = {'identity': {'mode': 'bot', 'bot': {'app_id': 'cli_demo'}}}
            with patch.object(gateway_module, 'health', return_value=(True, 'ok')):
                checks = self._checks(config, home, {'available': False,
                                                     'error': 'Expected Feishu bot'})
            self.assertEqual(checks['authenticated']['status'], 'fail')

    def test_token_with_group_bits_is_treated_as_leaked(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            token = gateway_module.token_path(home)
            token.write_text('a' * 48 + '\n')
            os.chmod(token, 0o644)
            config = {'identity': {'mode': 'bot', 'bot': {'app_id': 'cli_demo'}}}
            with patch.object(gateway_module, 'health', return_value=(True, 'ok')):
                checks = self._checks(config, home, {'appId': 'cli_demo', 'available': True,
                                                     'tokenStatus': 'valid'})
            self.assertEqual(checks['ingest_token']['status'], 'fail')


class GatewayHealthProbeTests(unittest.TestCase):
    def test_unreachable_port_reports_reason(self):
        ok, detail = gateway_module.health(1, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn('127.0.0.1:1', detail)


if __name__ == '__main__':
    unittest.main()
