"""身份相关通道判据：命令通道（两种身份各自语义）+ doctor 的 sidecar 硬判据。"""
import threading
import unittest
from unittest.mock import patch

from bridge import cli, host
from bridge.identity.bot import BotIdentity
from bridge.identity.user import UserIdentity


def bot_config(**extra):
    base = {'target_open_id': 'ou_owner', 'lark_binary': '/fake',
            'identity': {'mode': 'bot', 'owner_open_ids': ['ou_owner'],
                         'bot': {'app_id': 'cli_demo', 'bot_open_id': 'ou_bot'}}}
    base.update(extra)
    return base


class CommandChannelTests(unittest.TestCase):
    """命令通道：as_user=自聊白名单；as_bot=与机器人的私聊。语义相同（主人才算），实现不同。"""

    def test_user_keeps_self_chat_whitelist_plus_p2p(self):
        identity = UserIdentity({'target_open_id': 'ou_owner', 'lark_binary': '/fake'}, threading.Event())
        self.assertTrue(identity.command_chat_ok('oc_self', self_chat_ids=['oc_self']))
        self.assertTrue(identity.command_chat_ok('oc_dm', self_chat_ids=[], known_group=False),
                        '不在群册里的私聊＝与桥的一对一，个人账号一直这么用')
        self.assertFalse(identity.command_chat_ok('oc_group', self_chat_ids=[], known_group=True))

    def test_bot_only_accepts_direct_message(self):
        identity = BotIdentity(bot_config(), threading.Event())
        self.assertTrue(identity.command_chat_ok('oc_dm', chat_type='p2p'))
        self.assertFalse(identity.command_chat_ok('oc_group', chat_type='group'),
                         '群里的命令一律不执行（与 as_user 同一语义）')
        self.assertTrue(identity.command_chat_ok('oc_unknown'), '没有 chat_type 时不在群册即视为私聊')

    def test_self_sender_id_differs_by_identity(self):
        user = UserIdentity({'target_open_id': 'ou_owner', 'lark_binary': '/fake'}, threading.Event())
        bot = BotIdentity(bot_config(), threading.Event())
        self.assertEqual(user.self_sender_id(), 'ou_owner')
        self.assertEqual(bot.self_sender_id(), 'cli_demo', '机器人发帖的 sender.id 是 app_id')


class DoctorSidecarTests(unittest.TestCase):
    """bot 模式下 doctor 必须硬判 sidecar：通道写"ws"但单元没起来＝fail。"""

    def _checks(self, config):
        return cli._doctor_checks({'pid': 1, 'heartbeat_age_seconds': 1.0}, {}, config, True)

    def test_bot_mode_reports_sidecar_missing_as_fail(self):
        with patch.object(host, 'service_state', return_value={'loaded': False, 'pid': None}):
            checks = {row['name']: row for row in self._checks(bot_config())}
        self.assertIn('identity_sidecar', checks)
        self.assertEqual(checks['identity_sidecar']['status'], 'fail')

    def test_bot_mode_sidecar_loaded_is_ok(self):
        with patch.object(host, 'service_state', return_value={'loaded': True, 'pid': 42}):
            checks = {row['name']: row for row in self._checks(bot_config())}
        self.assertEqual(checks['identity_sidecar']['status'], 'ok')
        self.assertIn('pid=42', checks['identity_sidecar']['detail'])

    def test_user_mode_has_no_sidecar_row(self):
        with patch.object(host, 'service_state', return_value={'loaded': False, 'pid': None}):
            checks = {row['name']: row for row in self._checks(
                {'target_open_id': 'ou_owner', 'lark_binary': '/fake'})}
        self.assertNotIn('identity_sidecar', checks, 'user 模式不装 sidecar，也不该有这一行')

    def test_conflicting_config_fails_doctor(self):
        config = {'target_open_id': 'ou_owner', 'lark_binary': '/fake',
                  'identity': {'mode': 'user', 'bot': {'app_id': 'cli_demo'}}}
        checks = {row['name']: row for row in self._checks(config)}
        self.assertEqual(checks['identity']['status'], 'fail')
        self.assertIn('一台机器只能有一种身份', checks['identity']['detail'])


if __name__ == '__main__':
    unittest.main()
