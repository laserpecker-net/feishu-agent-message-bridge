"""PR-1 判据：身份层是"一种机器一种身份"，搬移后行为不变。

这些用例**只测新加的东西**（抽象/工厂/兼容壳），不改动既有的 1597 条。
"""
import pathlib
import threading
import unittest

from bridge import transport as transport_module
from bridge.identity import Identity, TransportError, UserIdentity, build_identity
from bridge.identity.factory import identity_mode

REPO = pathlib.Path(__file__).resolve().parent.parent


class FactoryTests(unittest.TestCase):
    def test_old_config_without_identity_block_defaults_to_user(self):
        identity = build_identity({}, threading.Event())
        self.assertIsInstance(identity, UserIdentity)
        self.assertIsInstance(identity, Identity)
        self.assertEqual(identity.mode, 'user')

    def test_mode_is_normalised_and_validated(self):
        self.assertEqual(identity_mode({'identity': {'mode': ' USER '}}), 'user')
        with self.assertRaises(RuntimeError) as caught:
            identity_mode({'identity': {'mode': 'robot'}})
        self.assertIn('identity.mode', str(caught.exception))

    def test_two_identities_in_one_config_break_startup(self):
        """硬原则：一台机器只有一种身份。既配 user 又配 bot.app_id → 起不来。"""
        config = {'target_open_id': 'ou_x', 'identity': {'mode': 'user', 'bot': {'app_id': 'cli_x'}}}
        with self.assertRaises(RuntimeError) as caught:
            build_identity(config, threading.Event())
        message = str(caught.exception)
        self.assertIn('一台机器只能有一种身份', message)
        self.assertIn('cli_x', message)

    def test_bot_mode_instantiates_after_pr2(self):
        # PR-2 起 as_bot 已实现：这里从"必须拒绝"改成"必须能实例化"。
        # 语义变化点只有这一处（工厂从报错改成返回 BotIdentity），其余判据不变。
        identity = build_identity({'identity': {'mode': 'bot', 'bot': {'app_id': 'cli_' + 'x' * 14}}})
        self.assertEqual(identity.mode, 'bot')
        self.assertFalse(identity.needs_self_guard)


class UserIdentityTests(unittest.TestCase):
    def make(self, **extra):
        config = {'target_open_id': 'ou_owner', 'signature': '署名'}
        config.update(extra)
        return UserIdentity(config, threading.Event())

    def test_mode_flags_are_explicit(self):
        identity = self.make()
        self.assertEqual(identity.mode, 'user')
        self.assertTrue(identity.needs_self_guard)
        self.assertIsInstance(identity.stop, threading.Event, '停止事件不能被同名方法遮住')

    def test_owner_and_mention_follow_target_open_id(self):
        identity = self.make()
        self.assertTrue(identity.is_owner('ou_owner'))
        self.assertFalse(identity.is_owner('ou_other'))
        self.assertFalse(identity.is_owner(None))
        message = {'mentions': [{'id': 'ou_owner'}]}
        self.assertTrue(identity.is_mentioned(message))
        self.assertFalse(identity.is_mentioned({'mentions': []}))

    def test_describe_reports_mode_and_masks_identity(self):
        rows = dict((name, (value, ok)) for name, value, ok in self.make().describe())
        self.assertIn('identity', rows)
        value, ok = rows['identity']
        self.assertIn('mode=user', value)
        self.assertNotIn('ou_owner', value, '体检输出里不该出现完整 open_id')
        self.assertTrue(ok)

    def test_start_stop_are_noops_for_user_identity(self):
        identity = self.make()
        self.assertIsNone(identity.start_channel())
        self.assertIsNone(identity.stop_channel())

    def test_whoami_exposes_mode_and_owner(self):
        who = self.make().whoami()
        self.assertEqual(who['mode'], 'user')
        self.assertEqual(who['open_id'], 'ou_owner')


class CompatibilityShellTests(unittest.TestCase):
    def test_transport_alias_is_the_same_class(self):
        self.assertIs(transport_module.Transport, UserIdentity)
        self.assertIs(transport_module.TransportError, TransportError)

    def test_shim_keeps_module_level_helpers(self):
        for name in ('mentioned', 'message_text', 'message_version', 'verify_reply',
                     'verify_reply_reason', 'canonical_text', 'mention_ids',
                     'SEARCH_MIN_INTERVAL_SECONDS'):
            self.assertTrue(hasattr(transport_module, name), name)


class NoModeBranchingTests(unittest.TestCase):
    def test_business_code_never_branches_on_identity_mode(self):
        """业务代码里出现 ``mode == 'bot'`` 就说明差异该收进实现类。"""
        offenders = []
        identity_dir = REPO / 'bridge' / 'identity'
        candidates = [p for p in list((REPO / 'bridge').rglob('*.py')) + list(REPO.glob('*.py'))
                      if identity_dir not in p.parents]
        for path in candidates:
            text = path.read_text(encoding='utf-8')
            for pattern in ("mode == 'bot'", 'mode=="bot"', "mode == \"bot\""):
                if pattern in text:
                    offenders.append('%s → %s' % (path.relative_to(REPO), pattern))
        self.assertEqual([], offenders)


if __name__ == '__main__':
    unittest.main()
