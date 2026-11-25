import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import consumer_runtime
import consumer_safety


class WorkspaceTests(unittest.TestCase):
    def test_none_and_empty_mean_no_workspace(self):
        self.assertIsNone(consumer_runtime.resolve_workspace(None))
        self.assertIsNone(consumer_runtime.resolve_workspace(''))

    def test_absolute_existing_directory_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(consumer_runtime.resolve_workspace(directory),
                             str(Path(directory).resolve()))

    def test_relative_or_missing_workspace_is_rejected(self):
        with self.assertRaises(SystemExit):
            consumer_runtime.resolve_workspace('relative/path')
        with self.assertRaises(SystemExit):
            consumer_runtime.resolve_workspace('/definitely/missing/workspace-dir')

    def test_default_home_matches_platform(self):
        home = consumer_runtime.default_home()
        self.assertTrue(str(home).endswith('FeishuPersonalAgent')
                        or str(home).endswith('.feishu-personal-agent'))


class GateTests(unittest.TestCase):
    """引擎 A 退役后，这几个门禁必须在引擎 B 里继续生效。"""

    def gate(self, home, identity=None):
        return consumer_runtime.runtime_gate(home, identity)

    def write(self, home, name, payload):
        (Path(home) / name).write_text(json.dumps(payload))

    def test_pause_stops_claiming(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.write(home, 'config.json', {'sending_enabled': False, 'target_open_id': 'ou_self'})
            self.assertEqual(self.gate(home, 'ou_self'), 'sending_disabled')

    def test_identity_change_stops_claiming(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.write(home, 'config.json', {'sending_enabled': True, 'target_open_id': 'ou_other'})
            self.assertEqual(self.gate(home, 'ou_self'), 'identity_changed')

    def test_hold_stops_claiming(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.write(home, 'config.json', {'sending_enabled': True, 'target_open_id': 'ou_self'})
            self.write(home, 'hold.json', {'active': True, 'since_ms': 1})
            self.assertEqual(self.gate(home, 'ou_self'), 'held')

    def test_unreadable_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertEqual(self.gate(home, None), 'config_unreadable')
            (home / 'config.json').write_text('{not json')
            self.assertEqual(self.gate(home, None), 'config_unreadable')

    def test_running_state_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.write(home, 'config.json', {'sending_enabled': True, 'target_open_id': 'ou_self'})
            self.write(home, 'hold.json', {'active': False})
            self.assertIsNone(self.gate(home, 'ou_self'))

    def test_identity_snapshot_matches_startup_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertIsNone(consumer_runtime.identity_at_start(home))
            self.write(home, 'config.json', {'target_open_id': 'ou_self'})
            self.assertEqual(consumer_runtime.identity_at_start(home), 'ou_self')


class OwnerNameTests(unittest.TestCase):
    """正文自称只认「这台机器当前登录的飞书账号」：没有配置项、没有人名默认值。"""

    def write(self, home, name, payload):
        (Path(home) / name).write_text(json.dumps(payload))

    def test_name_comes_from_the_verified_account_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.write(home, 'status.json', {'identity': {'userName': ' 张三 ', 'openId': 'ou_x'}})
            self.assertEqual(consumer_runtime.owner_name_at(home), '张三')
            # 账号换了，下一条回复就跟着换：不存在"安装时写死"的副本
            self.write(home, 'status.json', {'identity': {'userName': '别人的账号'}})
            self.assertEqual(consumer_runtime.owner_name_at(home), '别人的账号')

    def test_config_cannot_override_the_identity(self):
        """有人往 config 里塞 owner_name 也没用：那会成为第二条会漂移的事实。"""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.write(home, 'config.json', {'owner_name': '张三'})
            self.write(home, 'status.json', {'identity': {'userName': '张三'}})
            self.assertEqual(consumer_runtime.owner_name_at(home), '张三')

    def test_missing_or_broken_sources_mean_no_name(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertIsNone(consumer_runtime.owner_name_at(home))              # 什么都没有
            self.write(home, 'status.json', {'identity': {'userName': ''}})
            self.assertIsNone(consumer_runtime.owner_name_at(home))              # 空姓名
            self.write(home, 'status.json', {'identity': {'openId': 'ou_x'}})
            self.assertIsNone(consumer_runtime.owner_name_at(home))              # 无 userName
            (home / 'status.json').write_text('{not json')
            self.assertIsNone(consumer_runtime.owner_name_at(home))              # 坏 JSON
            (home / 'status.json').write_text(json.dumps(['列表']))
            self.assertIsNone(consumer_runtime.owner_name_at(home))              # 非对象


class TemplateSafetyTests(unittest.TestCase):
    def run_check(self, workspace=None):
        argv = ['consumer.py', '--consumer', 'test-main', '--check']
        if workspace:
            argv += ['--workspace', workspace]
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stdout(output):
            consumer_runtime.main(lambda home: (lambda payload, event, stop: 'unused'))
        return output.getvalue()

    def test_check_reports_capability_model_without_claiming(self):
        output = self.run_check()
        self.assertIn('no bridge event was claimed', output)
        self.assertIn('capabilities=', output)
        caps = json.loads(output.split('capabilities=', 1)[1].strip())
        self.assertEqual(caps, consumer_safety.ROLE_CAPABILITIES['replier'])

    def test_check_includes_resolved_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_check(directory)
            self.assertIn('workspace=%s' % os.path.realpath(directory), output)

    def test_check_rejects_native_windows_before_building_any_agent(self):
        built, parsed = [], []
        with mock.patch.object(sys, 'argv', ['consumer.py', '--check']), \
                mock.patch.object(consumer_runtime, 'require_posix_runtime',
                                  side_effect=SystemExit('native Windows')), \
                mock.patch.object(consumer_runtime.argparse, 'ArgumentParser',
                                  side_effect=lambda *a, **k: parsed.append(True)):
            with self.assertRaises(SystemExit):
                consumer_runtime.main(lambda home: built.append(home))
        self.assertEqual(built, [])
        self.assertEqual(parsed, [])  # the guard runs before any argument parsing

    def test_finalize_answer_is_the_outbound_guard(self):
        # The runtime must not depend on adapters remembering to redact.
        self.assertEqual(consumer_safety.finalize_answer('password: ' + 'a' * 24),
                         consumer_safety.BLOCKED_REPLY)
        with self.assertRaises(ValueError):
            consumer_safety.finalize_answer('   ')


if __name__ == '__main__':
    unittest.main()


class TopLevelCommandTests(unittest.TestCase):
    """`notify`/`teach` 是顶层命令，其余走 `events` 子命令。

    2026-11-06 踩过：只把 notify 放行，`teach add` 被拼成 `feishu-bridge events teach add …`，
    非零退出又被 try/except 吞掉——模型说"记下了"，教材一个字没写（真机验证时才暴露）。
    """

    def argv_for(self, args):
        seen = {}

        class Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ''

        def fake_run(argv, **kwargs):
            seen['argv'] = list(argv)
            return Result()

        with mock.patch('consumer_runtime.subprocess.run', side_effect=fake_run):
            consumer_runtime.command('feishu-bridge', '/tmp/home', args, body=None)
        return seen['argv']

    def test_every_action_the_runtime_runs_is_a_whitelisted_top_level_command(self):
        # 判据从**真实调用点**推导，不背常量：谁将来加第五个动作，忘了进白名单这里就红。
        # （2026-11-06 两次同样的坑：teach add、remind add 都被拼成 `events … add`。）
        captured = []

        def call(argv, body=None):
            captured.append(list(argv))
            return {'ok': True, 'id': 't-0007', 'name': '汇报先给结论'}

        event = {'id': 1, 'payload': {'message_id': 'om_x', 'chat_id': 'oc_x'}}
        consumer_runtime.add_teaching(call, event, {'kind': 'principle', 'text': '正文', 'name': '名'})
        consumer_runtime.revert_teaching(call, event, 't-0007')
        consumer_runtime.set_reminder(call, event, {'at': '21:40', 'text': '正文', 'name': '名'})
        consumer_runtime.cancel_reminder(call, event, '喝水')
        self.assertEqual([argv[0] for argv in captured], ['teach', 'teach', 'remind', 'remind'])
        for argv in captured:
            with self.subTest(argv=argv[0]):
                self.assertIn(argv[0], consumer_runtime.TOP_LEVEL_COMMANDS)
                self.assertNotIn('events', argv)

    def test_top_level_commands_are_not_prefixed_with_events(self):
        for name in ('notify', 'teach', 'remind'):
            with self.subTest(command=name):
                argv = self.argv_for([name, 'add'])
                self.assertEqual(argv[0], 'feishu-bridge')
                self.assertNotIn('events', argv, argv)
                self.assertEqual([part for part in argv if part == name], [name])
                self.assertIn('--home', argv)

    def test_event_commands_still_get_the_events_prefix(self):
        argv = self.argv_for(['claim', '--consumer', 'c'])
        self.assertEqual([part for part in argv if part == 'events'], ['events'])
