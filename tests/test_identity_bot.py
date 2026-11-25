"""as_bot 身份的接口契约（不依赖真飞书、不依赖 Node）。

判据都对着设计稿 §1 的接口来：谁在说话不同，**语义相同**。
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from bridge.identity import factory
from bridge.identity.base import Identity, TransportError
from bridge.identity.bot import BotIdentity
from bridge.identity.user import UserIdentity

APP_ID = 'cli_demo0000000000'


def bot_config(**bot):
    payload = {'app_id': APP_ID}
    payload.update(bot)
    return {'target_open_id': 'ou_owner', 'identity': {'mode': 'bot', 'bot': payload}}


class BotInstantiationTests(unittest.TestCase):
    def test_mode_bot_instantiates_bot_identity(self):
        identity = factory.build_identity(bot_config())
        self.assertIsInstance(identity, BotIdentity)
        self.assertIsInstance(identity, Identity)
        self.assertEqual(identity.mode, 'bot')
        self.assertFalse(identity.needs_self_guard, '机器人不会自己说话，不需要 own_message 守卫')

    def test_mode_user_still_instantiates_user_identity(self):
        identity = factory.build_identity({'target_open_id': 'ou_owner'})
        self.assertIsInstance(identity, UserIdentity)

    def test_bot_without_app_id_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            factory.build_identity({'identity': {'mode': 'bot', 'bot': {}}})

    def test_conflicting_identities_still_refused(self):
        # 一台机器一种身份：user 模式却带着 bot 配置 → 启动失败（设计稿 §5）
        with self.assertRaises(RuntimeError):
            factory.build_identity({'identity': {'mode': 'user', 'bot': {'app_id': APP_ID}}})


class BotContractTests(unittest.TestCase):
    def setUp(self):
        self.identity = BotIdentity(bot_config(bot_open_id='ou_bot'))

    def test_whoami_and_describe_are_masked(self):
        who = self.identity.whoami()
        self.assertEqual(who['mode'], 'bot')
        self.assertEqual(who['app_id'], APP_ID)
        rows = dict((name, (value, ok)) for name, value, ok in self.identity.describe())
        self.assertIn('mode=bot', rows['identity'][0])
        self.assertNotIn(APP_ID, rows['identity'][0], 'describe 里不能出现完整 app_id')
        self.assertIn('ws', rows['identity_channel'][0])

    def test_ingested_group_message_counts_as_mentioned(self):
        # 长连接只在"机器人被 @"时投递群消息，不用再猜一遍
        self.assertTrue(self.identity.is_mentioned({'via': 'ingest', 'mentions': []}))

    def test_mention_is_judged_by_bot_open_id(self):
        message = {'mentions': [{'id': {'open_id': 'ou_bot'}, 'name': 'bot'}]}
        self.assertTrue(self.identity.is_mentioned(message))
        other = {'mentions': [{'id': {'open_id': 'ou_someone'}, 'name': 'x'}]}
        self.assertFalse(self.identity.is_mentioned(other))

    def test_owner_is_configured_owner_not_self(self):
        identity = BotIdentity(bot_config(owner_open_ids=['ou_owner']))
        self.assertTrue(identity.is_owner('ou_owner'))
        self.assertFalse(identity.is_owner('ou_someone'))
        self.assertFalse(identity.is_owner(None))

    def test_bot_has_no_self_chat_and_no_search(self):
        # 这两个是 as_user 专有的入口，bot 下必须**明确报错**，不做静默降级
        with self.assertRaises(TransportError):
            self.identity.self_chat_id()
        with self.assertRaises(TransportError):
            self.identity.search(0, 1)

    def test_reconcile_collects_window_and_filters_mentions(self):
        calls = []

        def fake_inventory():
            return [{'chat_id': 'oc_1'}, {'chat_id': 'oc_2'}]

        def fake_chat_messages(chat_id, start_ms, end_ms):
            calls.append((chat_id, start_ms, end_ms))
            if chat_id == 'oc_1':
                return ([{'message_id': 'om_' + 'a' * 20,
                          'mentions': [{'id': {'open_id': 'ou_bot'}}]},
                         {'message_id': 'om_' + 'b' * 20, 'mentions': []}], True)
            raise TransportError('perm denied', 'authorization', retryable=False)

        self.identity.inventory = fake_inventory
        self.identity.chat_messages = fake_chat_messages
        result = self.identity.collect('live', 1000, 2000)
        self.assertEqual(result['ids'], ['om_' + 'a' * 20])
        self.assertIsNone(result['next_token'])
        self.assertEqual([call[0] for call in calls], ['oc_1', 'oc_2'], '一个会话失败不该拖垮整窗')

    def test_sidecar_argv_points_at_sidecar_index(self):
        argv = self.identity.sidecar_argv()
        self.assertEqual(argv[0], 'node')
        self.assertTrue(argv[1].endswith('sidecar/index.js'))


class SidecarAssetsTests(unittest.TestCase):
    def test_sidecar_syntax_and_manifest(self):
        root = pathlib.Path(__file__).resolve().parent.parent / 'sidecar'
        manifest = json.loads((root / 'package.json').read_text())
        self.assertIn('@larksuiteoapi/node-sdk', manifest['dependencies'])
        script = root / 'index.js'
        self.assertIn('im.message.receive_v1', script.read_text())
        node = shutil.which('node')
        if not node:  # pragma: no cover - 环境没有 node 时如实跳过
            self.skipTest('node 不在 PATH，跳过语法检查')
        check = subprocess.run([node, '--check', str(script)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == '__main__':
    unittest.main()
