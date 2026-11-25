import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from bridge import cli
from bridge import release as brelease
from bridge.reply import render_reply
from bridge.store import Store, reply_uuid
from bridge import gateway
from bridge.transport import TransportError


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


PRINT_OUTPUT = '''gui/501/io.feishu-agent.bridge = {
\tactive count = 1
\tpath = /home/user/Library/LaunchAgents/io.feishu-agent.bridge.plist
\ttype = LaunchAgent
\tstate = running
\tpid = 62026
\texit timeout = 90
\tlast terminating signal = Killed: 9
\truns = 2
\tresource coalition = {
\t\tstate = active
\t}
}
'''


class FakeLaunchd:
    """最小 launchd 状态机：记录调用序列，模拟 bootout/enable/bootstrap/kickstart。"""

    def __init__(self, loaded=True, pid=42):
        self.loaded = loaded
        self.pid = pid if loaded else None
        self.calls = []
        self.bootstrap_failures = 0

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        command = args[1]
        if command == 'bootout':
            self.loaded, self.pid = False, None
        elif command == 'bootstrap':
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return FakeResult(1, stderr='Bootstrap failed: 5: Input/output error')
            self.loaded, self.pid = True, 4242
        elif command == 'kickstart':
            self.loaded, self.pid = True, self.pid or 4242
        return FakeResult(0)

    def status(self, timeout=10):
        result = {'loaded': self.loaded, 'label': cli.LABEL}
        if self.loaded and self.pid:
            result.update(pid=self.pid, state='running')
        return result

    def names(self):
        return [call[1] for call in self.calls]

    def last_kickstart(self):
        for call in reversed(self.calls):
            if call[1] == 'kickstart':
                return call
        return None


class LaunchStatusTests(unittest.TestCase):
    def test_parses_state_pid_and_terminating_signal(self):
        with patch.object(cli.subprocess, 'run', return_value=FakeResult(0, PRINT_OUTPUT)):
            parsed = cli.launch_status()
        self.assertTrue(parsed['loaded'])
        self.assertEqual(parsed['label'], cli.LABEL)
        self.assertEqual(parsed['state'], 'running')
        self.assertEqual(parsed['pid'], 62026)
        self.assertEqual(parsed['last_terminating_signal'], 'Killed: 9')
        self.assertNotIn('last_exit_code', parsed)

    def test_parses_last_exit_code(self):
        output = PRINT_OUTPUT.replace('\tlast terminating signal = Killed: 9\n',
                                      '\tlast terminating signal = Killed: 9\n\tlast exit code = 78\n')
        with patch.object(cli.subprocess, 'run', return_value=FakeResult(0, output)):
            parsed = cli.launch_status()
        self.assertEqual(parsed['last_exit_code'], 78)

    def test_never_exited_is_preserved_as_text(self):
        output = PRINT_OUTPUT.replace('\tlast terminating signal = Killed: 9\n',
                                      '\tlast terminating signal = Killed: 9\n'
                                      '\tlast exit code = (never exited)\n')
        with patch.object(cli.subprocess, 'run', return_value=FakeResult(0, output)):
            parsed = cli.launch_status()
        self.assertEqual(parsed['last_exit_code'], '(never exited)')

    def test_nested_state_does_not_override_top_level_state(self):
        nested_first = 'launchd = {\n\tresource coalition = {\n\t\tstate = active\n\t}\n\tstate = not running\n}\n'
        with patch.object(cli.subprocess, 'run', return_value=FakeResult(0, nested_first)):
            parsed = cli.launch_status()
        self.assertEqual(parsed['state'], 'not running')

    def test_unloaded_service_reports_loaded_false(self):
        with patch.object(cli.subprocess, 'run', return_value=FakeResult(1, stderr='Could not find service')):
            parsed = cli.launch_status()
        self.assertFalse(parsed['loaded'])
        self.assertIn('Could not find service', parsed['error'])

    def test_timeout_returns_loaded_false_with_error(self):
        with patch.object(cli.subprocess, 'run', side_effect=subprocess.TimeoutExpired('launchctl', 10)):
            parsed = cli.launch_status(timeout=10)
        self.assertFalse(parsed['loaded'])
        self.assertIn('TimeoutExpired', parsed['error'])

    def test_oserror_returns_loaded_false_with_error(self):
        with patch.object(cli.subprocess, 'run', side_effect=OSError('launchctl missing')):
            parsed = cli.launch_status()
        self.assertFalse(parsed['loaded'])
        self.assertIn('launchctl missing', parsed['error'])


class LifecycleTests(unittest.TestCase):
    def lifecycle(self, command, launchd, home=None):
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', command]), \
                patch.object(cli, 'launch_status', side_effect=launchd.status), \
                patch.object(cli.subprocess, 'run', side_effect=launchd.run), \
                patch.object(cli.time, 'sleep'), \
                patch('builtins.print'):
            cli.main(home or directory)
        return launchd

    def test_restart_boots_out_waits_enables_bootstraps_and_kickstarts(self):
        launchd = self.lifecycle('restart', FakeLaunchd())
        self.assertEqual(launchd.names(), ['bootout', 'enable', 'bootstrap', 'kickstart'])

    def test_stop_only_boots_out(self):
        launchd = self.lifecycle('stop', FakeLaunchd())
        self.assertEqual(launchd.names(), ['bootout'])

    def test_stop_when_unloaded_is_a_noop(self):
        launchd = self.lifecycle('stop', FakeLaunchd(loaded=False))
        self.assertEqual(launchd.names(), [])

    def test_start_on_running_service_does_not_restart_without_drift(self):
        launchd = self.lifecycle('start', FakeLaunchd(loaded=True, pid=42))
        self.assertEqual(launchd.names(), ['enable'])

    def test_start_reloads_the_plist_when_installed_release_differs(self):
        # launchd 不会重读磁盘 plist：换过 release 时必须 bootout+bootstrap，而不是只 kickstart。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'installation.json').write_text(json.dumps({'release_id': 'newrelease'}))
            (home / 'status.json').write_text(json.dumps({'running_release': 'oldrelease'}))
            launchd = self.lifecycle('start', FakeLaunchd(loaded=True, pid=42), home=str(home))
        self.assertEqual(launchd.names(), ['bootout', 'enable', 'bootstrap'])

    def test_start_when_unloaded_bootstraps(self):
        launchd = self.lifecycle('start', FakeLaunchd(loaded=False))
        self.assertEqual(launchd.names(), ['enable', 'bootstrap'])

    def test_bootstrap_is_retried_until_it_succeeds(self):
        launchd = FakeLaunchd()
        launchd.bootstrap_failures = 2
        self.lifecycle('restart', launchd)
        self.assertEqual(launchd.names().count('bootstrap'), 3)
        self.assertIn('kickstart', launchd.names())

    def test_bootstrap_failure_gives_explicit_recovery_guidance(self):
        launchd = FakeLaunchd()
        launchd.bootstrap_failures = 3
        with self.assertRaises(SystemExit) as caught:
            self.lifecycle('restart', launchd)
        message = str(caught.exception)
        self.assertIn('重新加载失败', message)
        self.assertIn('feishu-bridge start', message)
        self.assertEqual(launchd.names().count('bootstrap'), 3)
        self.assertNotIn('kickstart', launchd.names())

    def test_wait_timeout_exits_with_guidance(self):
        launchd = FakeLaunchd()
        original_run = launchd.run

        def bootout_never_finishes(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[1] == 'bootout':
                launchd.loaded, launchd.pid = True, 42
            return result

        clock = {'now': 0.0}

        def monotonic():
            clock['now'] += 1.0
            return clock['now']

        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', 'stop']), \
                patch.object(cli, 'launch_status', side_effect=launchd.status), \
                patch.object(cli.subprocess, 'run', side_effect=bootout_never_finishes), \
                patch.object(cli.time, 'sleep'), \
                patch.object(cli.time, 'monotonic', side_effect=monotonic), \
                patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                cli.main(directory)
        self.assertIn('仍在停止中', str(caught.exception))
        self.assertNotIn('enable', launchd.names())

    def test_mutating_command_respects_cli_lock(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', 'restart']), \
                patch.object(cli.fcntl, 'flock', side_effect=BlockingIOError('busy')), \
                patch.object(cli.subprocess, 'run') as run, \
                patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                cli.main(directory)
        self.assertIn('另一个 feishu-bridge CLI 命令正在执行', str(caught.exception))
        run.assert_not_called()



class NotifyTests(unittest.TestCase):
    """`feishu-bridge notify` 是"答不了就私聊问本人"的出口：去重、限额、暂停都要生效。"""

    def make_home(self, directory, **overrides):
        home = Path(directory)
        config = {'target_open_id': 'ou_self', 'sending_enabled': True, 'signature': '署名'}
        config.update(overrides)
        (home / 'config.json').write_text(json.dumps(config))
        return home

    def run_notify(self, home, argv=(), stdin='需要你确认：A 还是 B？'):
        stream = io.StringIO()
        fake = mock.Mock()
        fake.send_dm.return_value = {'message_id': 'om_dm1', 'chat_id': 'oc_dm'}
        with patch('sys.argv', ['feishu-bridge', '--home', str(home), 'notify'] + list(argv)), \
                patch('sys.stdin', io.StringIO(stdin)), \
                patch('bridge.transport.Transport', return_value=fake), \
                contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(Path(home)))
        return json.loads(stream.getvalue()), fake, caught

    def test_sends_a_private_message_with_the_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(directory)
            report, fake, caught = self.run_notify(home)
        self.assertEqual(caught.exception.code, 0)
        self.assertTrue(report['sent'])
        self.assertEqual(report['message_id'], 'om_dm1')
        body = fake.send_dm.call_args[0][1]
        self.assertIn('需要你确认', body)
        self.assertTrue(body.rstrip().endswith('署名'))

    def test_dry_run_checks_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(directory)
            report, fake, _ = self.run_notify(home, ['--dry-run'])
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['decision'], 'send')
        fake.send_dm.assert_not_called()

    def test_the_same_notification_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(directory)
            self.run_notify(home)
            report, fake, _ = self.run_notify(home)
        self.assertFalse(report['sent'])
        self.assertEqual(report['decision'], 'duplicate')
        fake.send_dm.assert_not_called()

    def test_paused_sending_refuses_to_notify(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(directory, sending_enabled=False)
            fake = mock.Mock()
            with patch('sys.argv', ['feishu-bridge', '--home', str(home), 'notify']), \
                    patch('sys.stdin', io.StringIO('需要你确认')), \
                    patch('bridge.transport.Transport', return_value=fake), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(str(home))
        self.assertIn('发送已暂停', str(caught.exception))
        fake.send_dm.assert_not_called()

    def test_body_comes_from_stdin_not_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(directory)
            with patch('sys.argv', ['feishu-bridge', '--home', str(home), 'notify', '明文正文']), \
                    patch('sys.stdin', io.StringIO('从标准输入来的正文')), \
                    patch('bridge.transport.Transport', return_value=mock.Mock()), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main(str(Path(home)))


class RollbackTests(unittest.TestCase):
    def make_release(self, home, text):
        releases = home / 'releases'
        temp = releases / 'temp-copy'
        bridge = temp / 'bridge'
        bridge.mkdir(parents=True)
        (bridge / '__init__.py').write_text('')
        (bridge / 'cli.py').write_text(text)
        name = brelease.release_id(temp)
        temp.rename(releases / name)
        return name

    def fixture(self, root):
        home = Path(root)
        (home / 'releases').mkdir(parents=True)
        first = self.make_release(home, '# first\n')
        second = self.make_release(home, '# second\n')
        (home / 'current').symlink_to(home / 'releases' / first, target_is_directory=True)
        (home / 'installation.json').write_text(json.dumps({'release_id': first}))
        return home, first, second

    def test_list_reports_current_and_available_releases(self):
        with tempfile.TemporaryDirectory() as root:
            home, first, second = self.fixture(root)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                cli._rollback(home, ['--list'])
            data = json.loads(stream.getvalue())
        self.assertEqual(data['current'], first)
        self.assertEqual(sorted(data['releases']), sorted({first, second}))

    def test_switch_points_current_and_receipt_at_the_old_release(self):
        with tempfile.TemporaryDirectory() as root:
            home, first, second = self.fixture(root)
            with contextlib.redirect_stdout(io.StringIO()):
                cli._rollback(home, ['--to', second])
            receipt = json.loads((home / 'installation.json').read_text())
            resolved = (home / 'current').resolve().name
        self.assertEqual(resolved, second)
        self.assertEqual(receipt['release_id'], second)
        self.assertEqual(receipt['rolled_back_from'], first)

    def test_default_target_is_the_newest_other_release(self):
        with tempfile.TemporaryDirectory() as root:
            home, first, second = self.fixture(root)
            os.utime(home / 'releases' / second, (time.time() + 10, time.time() + 10))
            with contextlib.redirect_stdout(io.StringIO()):
                cli._rollback(home, [])
            resolved = (home / 'current').resolve().name
        self.assertEqual(resolved, second)

    def test_corrupt_release_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            home, first, second = self.fixture(root)
            (home / 'releases' / second / 'bridge' / 'cli.py').write_text('# tampered\n')
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()):
                cli._rollback(home, ['--to', second])
            resolved = (home / 'current').resolve().name
        self.assertIn('内容损坏', str(caught.exception))
        self.assertEqual(resolved, first)

    def test_release_id_covers_the_root_consumer_modules(self):
        """只改根级消费者模块（agent_prompt.py 等）也必须换 release id。

        指纹只算 bridge/*.py 时，回复正文的代码变了而 id 不变，「installed==running」就证明
        不了消费者跑的是哪份代码——这正是写死身份那次事故能闷好几轮升级的原因。
        """
        import re
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source'
            (source / 'bridge').mkdir(parents=True)
            (source / 'bridge' / 'service.py').write_text('# service\n')
            for name in brelease.CONSUMER_FILES:
                (source / name).write_text('# %s\n' % name)
            before = brelease.release_id(source)
            (source / 'agent_prompt.py').write_text('# agent_prompt changed\n')
            after = brelease.release_id(source)
            # release 目录（bridge/ + 复制的根级模块）算出来的 id 与源一致，回滚才能校验
            release = Path(root) / 'releases' / after
            (release / 'bridge').mkdir(parents=True)
            for path in brelease.runtime_files(source):
                target = release / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            released = brelease.release_id(release)
        self.assertNotEqual(before, after)
        self.assertEqual(released, after)
        self.assertTrue(re.match(r'^[0-9a-f]{16}$', after))

    def test_bad_release_id_is_rejected_before_touching_current(self):
        with tempfile.TemporaryDirectory() as root:
            home, first, _ = self.fixture(root)
            with self.assertRaises(SystemExit) as caught:
                cli._rollback(home, ['--to', '../../etc'])
            resolved = (home / 'current').resolve().name
        self.assertIn('格式不对', str(caught.exception))
        self.assertEqual(resolved, first)


    def test_main_routes_rollback_to_the_subcommand(self):
        with tempfile.TemporaryDirectory() as root:
            home, first, second = self.fixture(root)
            stream = io.StringIO()
            with patch('sys.argv', ['feishu-bridge', '--home', str(home), 'rollback', '--list']), \
                    contextlib.redirect_stdout(stream):
                cli.main(str(home))
            data = json.loads(stream.getvalue())
        self.assertEqual(data['current'], first)
        self.assertEqual(sorted(data['releases']), sorted({first, second}))


class StatusCommandTests(unittest.TestCase):
    def status_output(self, home, launch):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'status']), \
                patch.object(cli, 'launch_status', return_value=launch), \
                contextlib.redirect_stdout(stream):
            cli.main(str(home))
        return json.loads(stream.getvalue())

    def test_reports_update_pending_when_releases_differ(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            now = time.time()
            (home / 'status.json').write_text(json.dumps({'pid': 42, 'heartbeat_at': now, 'authenticated': True,
                                                          'last_search_at': now, 'running_release': 'old'}))
            (home / 'installation.json').write_text(json.dumps({'release_id': 'new'}))
            data = self.status_output(home, {'loaded': True, 'pid': 42, 'state': 'running'})
        self.assertTrue(data['update_pending'])
        self.assertEqual(data['installed_release'], 'new')

    def test_update_pending_is_false_without_running_release(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            now = time.time()
            (home / 'status.json').write_text(json.dumps({'pid': 42, 'heartbeat_at': now, 'authenticated': True,
                                                          'last_search_at': now}))
            (home / 'installation.json').write_text(json.dumps({'release_id': 'new'}))
            data = self.status_output(home, {'loaded': True, 'pid': 42, 'state': 'running'})
        self.assertFalse(data['update_pending'])

    def test_receiving_healthy_requires_small_collector_lag(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            now = time.time()
            (home / 'status.json').write_text(json.dumps({'pid': 42, 'heartbeat_at': now, 'authenticated': True,
                                                          'last_search_at': now,
                                                          'collector': {'watermark_lag_s': 900}}))
            data = self.status_output(home, {'loaded': True, 'pid': 42, 'state': 'running'})
        self.assertTrue(data['process_running'])
        self.assertFalse(data['receiving_healthy'])

    def test_receiving_healthy_when_collector_lag_is_small(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            now = time.time()
            (home / 'status.json').write_text(json.dumps({'pid': 42, 'heartbeat_at': now, 'authenticated': True,
                                                          'last_search_at': now,
                                                          'collector': {'watermark_lag_s': 12.5}}))
            data = self.status_output(home, {'loaded': True, 'pid': 42, 'state': 'running'})
        self.assertTrue(data['receiving_healthy'])

    def test_corrupt_status_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'status.json').write_text('{not json')
            data = self.status_output(home, {'loaded': False, 'error': 'Could not find service'})
        self.assertIn('launchd', data)
        self.assertFalse(data['process_running'])


class _FakeTransport:
    identity_payload = {'scopes': ['im:message']}
    inventory_payload = [{'chat_id': 'oc_1'}, {'chat_id': 'oc_2'}]

    def __init__(self, *args, **kwargs):
        pass

    def identity(self):
        return self.identity_payload

    def inventory(self):
        return self.inventory_payload


class _FailingTransport(_FakeTransport):
    def identity(self):
        raise TransportError('Authorization expired', 'authorization')



class DoctorTests(unittest.TestCase):
    def doctor_output(self, home):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'doctor']), \
                patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 7}), \
                contextlib.redirect_stdout(stream):
            cli.main(str(home))
        return json.loads(stream.getvalue())

    def test_doctor_prints_json_error_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))
            with patch('bridge.transport.Transport', _FailingTransport):
                data = self.doctor_output(home)
        self.assertIn('Authorization expired', data['error'])

    def test_doctor_includes_release_and_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x', 'sending_enabled': False}))
            (home / 'installation.json').write_text(json.dumps({'release_id': 'rel-1'}))
            (home / 'status.json').write_text(json.dumps({'stats': {'attention': {'inbox_needs_attention': 2}}}))
            with patch('bridge.transport.Transport', _FakeTransport):
                data = self.doctor_output(home)
        self.assertEqual(data['release_id'], 'rel-1')
        self.assertEqual(data['attention'], {'inbox_needs_attention': 2})
        self.assertEqual(data['joined_groups'], 2)
        self.assertNotIn('error', data)

    def test_doctor_reply_identity_comes_from_the_account_identity(self):
        """自称/署名判据必须由本机账号身份生成——代码与 config 里都不许有人名默认值。"""
        class WithName(_FakeTransport):
            identity_payload = {'userName': '张三', 'openId': 'ou_x', 'tokenStatus': 'valid'}

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x', 'signature': '来自张三的AI助手'}))
            with patch('bridge.transport.Transport', WithName):
                data = self.doctor_output(home)
        check = [item for item in data['checks'] if item['name'] == 'reply_identity']
        self.assertEqual(len(check), 1)
        self.assertEqual(check[0]['status'], 'ok')
        self.assertIn('自称「张三的 AI 助手」（来源：本机飞书账号身份）', check[0]['detail'])
        self.assertIn('署名「来自张三的AI助手」', check[0]['detail'])

    def test_doctor_prints_the_signature_that_will_actually_be_sent(self):
        """没配署名时 doctor 要打印**渲染后**的署名（带账号名），不是原始模板。"""

        class WithName(_FakeTransport):
            identity_payload = {'userName': '张三', 'openId': 'ou_x', 'tokenStatus': 'valid'}

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))
            with patch('bridge.transport.Transport', WithName):
                data = self.doctor_output(home)
        check = [item for item in data['checks'] if item['name'] == 'reply_identity'][0]
        self.assertEqual(check['status'], 'ok')
        self.assertIn('署名「此回复信息来自张三的 AI 助手」（内置模板）', check['detail'])

    def test_doctor_says_what_to_do_when_the_private_channel_is_off(self):
        """私聊通道默认开通，所以"没开"必须给出这台机器上唯一能做的下一步。"""
        cases = {
            'no_p2p_scope': ('warn', 'im:message.p2p_msg:get_as_user'),
            'detecting': ('info', '正在认本人私聊会话'),
            'corrupt': ('warn', 'teach.json'),
            'disabled': ('info', 'teach disable'),
            'no_self_chat': ('warn', '给自己发一条消息'),
        }
        for reason, (level, needle) in cases.items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))
                (home / 'status.json').write_text(json.dumps(
                    {'teach_channel': {'enabled': False, 'reason': reason}}))
                with patch('bridge.transport.Transport', _FakeTransport):
                    data = self.doctor_output(home)
                check = [item for item in data['checks'] if item['name'] == 'teach_channel'][0]
                self.assertEqual(check['status'], level)
                self.assertIn(needle, check['detail'])

    def test_doctor_reports_missing_config_as_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.doctor_output(Path(directory))
        self.assertIn('config.json', data['error'])


class DoctorAssertTests(unittest.TestCase):
    """doctor --assert 是 macOS / Linux 共用的「可自动化」体检入口：fail 才非零退出。"""

    def run_doctor(self, home, *extra):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'doctor'] + list(extra)), \
                contextlib.redirect_stdout(stream):
            try:
                code = cli.main(str(home))
            except SystemExit as error:
                code = error.code
        return code, json.loads(stream.getvalue())

    def home_with(self, directory, *, heartbeat_age=1, pid=5):
        home = Path(directory)
        (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))
        (home / 'installation.json').write_text(json.dumps({'release_id': 'rel-1'}))
        (home / 'status.json').write_text(json.dumps({
            'pid': pid, 'heartbeat_at': time.time() - heartbeat_age,
            'running_release': 'rel-1', 'mode': 'running',
            'workers': {'collector': True, 'details': True, 'sender': True}}))
        return home

    def test_healthy_host_passes_assert(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory)
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        self.assertEqual(data['verdict'], 'ok')
        self.assertEqual(code, 0)
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['service_running'], 'ok')
        self.assertEqual(names['heartbeat_fresh'], 'ok')

    def test_dead_service_fails_assert(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory, heartbeat_age=600, pid=5)
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['service_running'], 'fail')
        self.assertEqual(names['heartbeat_fresh'], 'fail')
        self.assertEqual(data['verdict'], 'fail')
        self.assertEqual(code, 1)

    def test_without_assert_exit_code_stays_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory, heartbeat_age=600)
            with patch.object(cli, 'launch_status', return_value={'loaded': False}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home)
        self.assertEqual(data['verdict'], 'fail')
        self.assertEqual(code, 0)

    def test_workers_missing_from_old_release_is_only_a_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory)
            (home / 'status.json').write_text(json.dumps({
                'pid': 5, 'heartbeat_at': time.time(), 'running_release': 'rel-1', 'mode': 'running'}))
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['workers_complete'], 'warn')
        self.assertEqual(code, 0)

    def test_incomplete_workers_fail_the_assertion(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory)
            (home / 'status.json').write_text(json.dumps({
                'pid': 5, 'heartbeat_at': time.time(), 'running_release': 'rel-1', 'mode': 'running',
                'workers': {'collector': True, 'details': False, 'sender': True}}))
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['workers_complete'], 'fail')
        self.assertEqual(code, 1)

    def test_consumer_offline_is_a_hard_failure(self):
        # 设计稿 §3.3：消费者不在线时消息停在 external_queued，doctor 必须判死，
        # 而不是让服务看起来一切正常（没有兜底回答者了）。
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory)
            store = Store(home / 'state.sqlite3')
            try:
                store.begin_scan('live', 0, 1)
                store.ingest_search_page('live', ['om_gap'], None, time.time() - 600)
                store.emit_event('message', 'message:om_gap', {'message_id': 'om_gap'},
                                 time.time() - 600)
            finally:
                store.close()
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['consumer_offline'], 'fail')
        self.assertEqual(code, 1)

    def test_a_warm_consumer_passes_the_offline_check(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory)
            store = Store(home / 'state.sqlite3')
            try:
                store.begin_scan('live', 0, 1)
                store.ingest_search_page('live', ['om_ok'], None, time.time())
                store.emit_event('message', 'message:om_ok', {'message_id': 'om_ok'}, time.time())
            finally:
                store.close()
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['consumer_offline'], 'ok')
        self.assertEqual(code, 0)
    def test_backlog_and_attention_surface_as_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.home_with(directory)
            (home / 'status.json').write_text(json.dumps({
                'pid': 5, 'heartbeat_at': time.time(), 'running_release': 'rel-1', 'mode': 'running',
                'workers': {'collector': True, 'details': True, 'sender': True},
                'stats': {'attention': {'inbox_needs_attention': 3, 'events_dead': 2,
                                        'oldest_attention_at': time.time() - 7200}}}))
            with patch.object(cli, 'launch_status', return_value={'loaded': True, 'pid': 5}), \
                    patch('bridge.transport.Transport', _FakeTransport):
                code, data = self.run_doctor(home, '--assert')
        names = {check['name']: check['status'] for check in data['checks']}
        self.assertEqual(names['attention_clear'], 'warn')
        self.assertEqual(names['backlog_age'], 'warn')
        self.assertEqual(code, 0)

class SelfChatCommandTests(unittest.TestCase):
    def test_add_list_remove_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))
            with patch('sys.argv', ['feishu-bridge', 'self-chat', '--add', 'oc_self']), \
                 patch('builtins.print'):
                cli.main(directory)
            config = json.loads((home / 'config.json').read_text())
            self.assertEqual(config['self_chat_ids'], ['oc_self'])
            with patch('sys.argv', ['feishu-bridge', 'self-chat', '--add', 'oc_two', '--add', 'oc_self']), \
                 patch('builtins.print'):
                cli.main(directory)
            self.assertEqual(json.loads((home / 'config.json').read_text())['self_chat_ids'],
                             ['oc_self', 'oc_two'])
            with patch('sys.argv', ['feishu-bridge', 'self-chat', '--remove', 'oc_self']), \
                 patch('builtins.print'):
                cli.main(directory)
            self.assertEqual(json.loads((home / 'config.json').read_text())['self_chat_ids'], ['oc_two'])
            with patch('sys.argv', ['feishu-bridge', 'self-chat', '--clear']), \
                 patch('builtins.print'):
                cli.main(directory)
            self.assertEqual(json.loads((home / 'config.json').read_text())['self_chat_ids'], [])

    def test_invalid_chat_id_is_rejected_without_touching_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = {'target_open_id': 'ou_x'}
            (home / 'config.json').write_text(json.dumps(original))
            with patch('sys.argv', ['feishu-bridge', 'self-chat', '--add', 'bad-id']), \
                 patch('builtins.print'):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(directory)
            self.assertIn('oc_', str(caught.exception))
            self.assertEqual(json.loads((home / 'config.json').read_text()), original)

    def test_missing_config_exits_with_hint(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('sys.argv', ['feishu-bridge', 'self-chat', '--add', 'oc_self']), \
             patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                cli.main(directory)
        self.assertIn('install.py', str(caught.exception))


class FriendlyCommandErrorTests(unittest.TestCase):
    def test_pause_without_config_exits_with_hint(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', 'pause']), \
                patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                cli.main(directory)
        self.assertIn('install.py', str(caught.exception))

    def test_pause_updates_sending_enabled(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', 'pause']), \
                patch('builtins.print'):
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'sending_enabled': True, 'target_open_id': 'ou_x'}))
            cli.main(directory)
            config = json.loads((home / 'config.json').read_text())
            leftovers = sorted(path.name for path in home.iterdir())
        self.assertFalse(config['sending_enabled'])
        self.assertEqual(leftovers, ['cli.lock', 'config.json'])

    def test_pause_lock_conflict_is_reported(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', 'pause']), \
                patch.object(cli.fcntl, 'flock', side_effect=BlockingIOError('busy')), \
                patch('builtins.print'):
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'sending_enabled': True}))
            with self.assertRaises(SystemExit) as caught:
                cli.main(directory)
        self.assertIn('另一个 feishu-bridge CLI 命令正在执行', str(caught.exception))

    def test_logs_without_home_exits_with_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing-home'
            with patch('sys.argv', ['feishu-bridge', 'logs']), patch('builtins.print'):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(str(missing))
        self.assertIn('install.py', str(caught.exception))

    def test_logs_prints_last_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'bridge.log').write_text('one\ntwo\nthree\n')
            stream = io.StringIO()
            with patch('sys.argv', ['feishu-bridge', 'logs']), contextlib.redirect_stdout(stream):
                cli.main(directory)
        self.assertEqual(stream.getvalue().strip().splitlines(), ['one', 'two', 'three'])


class BotsCommandTests(unittest.TestCase):
    """`feishu-bridge bots`：别的机器人 @ 本人要不要回（默认回）。"""

    def home(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        Path(home, 'config.json').write_text(json.dumps({'target_open_id': 'ou_target'}))
        return Path(home)

    def run_bots(self, home, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._bots(home, list(args))
        return json.loads(buffer.getvalue())

    def test_default_is_reply(self):
        home = self.home()
        self.assertTrue(self.run_bots(home, '--list')['reply_to_bots'])

    def test_disable_then_enable(self):
        home = self.home()
        report = self.run_bots(home, '--disable')
        self.assertEqual((report['reply_to_bots'], report['changed']), (False, True))
        self.assertIs(json.loads(Path(home, 'config.json').read_text())['reply_to_bots'], False)
        # 幂等：再关一次不算改动
        self.assertFalse(self.run_bots(home, '--disable')['changed'])
        report = self.run_bots(home, '--enable')
        self.assertEqual((report['reply_to_bots'], report['changed']), (True, True))
        self.assertNotIn('reply_to_bots', json.loads(Path(home, 'config.json').read_text()))


class LogsCommandTests(unittest.TestCase):
    def run_logs(self, home, *extra):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'logs'] + list(extra)), \
                contextlib.redirect_stdout(stream):
            cli.main(str(home))
        return stream.getvalue()

    def test_lines_option_limits_output(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'bridge.log').write_text('\n'.join('line%d' % i for i in range(20)) + '\n')
            out = self.run_logs(home, '--lines', '3')
        self.assertEqual(out.strip().splitlines(), ['line17', 'line18', 'line19'])

    def test_all_includes_backend_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'bridge.log').write_text('业务日志\n')
            (home / 'launchd.stderr.log').write_text('Traceback: ImportError\n')
            out = self.run_logs(home, '--all')
        self.assertIn('bridge.log', out)
        self.assertIn('launchd.stderr.log', out)
        self.assertIn('Traceback: ImportError', out)

    def test_missing_home_is_a_friendly_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as caught:
                self.run_logs(Path(directory) / 'nope')
        self.assertIn('install.py', str(caught.exception))


class HoldUpdatePendingGuardTests(unittest.TestCase):
    """还在跑旧 release 时说「已下班」是撒谎：必须拒绝并让人先 restart。"""

    def test_hold_refuses_when_running_release_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))
            (home / 'installation.json').write_text(json.dumps({'release_id': 'new-release'}))
            (home / 'status.json').write_text(json.dumps({'running_release': 'old-release'}))
            with patch('sys.argv', ['feishu-bridge', 'hold']), patch('builtins.print'):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(str(home))
        self.assertIn('restart', str(caught.exception))
        self.assertFalse((home / 'hold.json').exists())


class EventsCommandTests(unittest.TestCase):
    """`feishu-bridge events ...`：temp home + 真实 Store，全程离线（无 launchctl/network）。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)
        store = Store(str(self.home / 'state.sqlite3'))
        store.close()

    def tearDown(self):
        self._directory.cleanup()

    def open_store(self):
        return Store(str(self.home / 'state.sqlite3'))

    def run_events(self, *argv, stdin=None):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events'] + [str(arg) for arg in argv]), \
                contextlib.redirect_stdout(stream):
            if stdin is None:
                cli.main(str(self.home))
            else:
                with patch('sys.stdin', io.StringIO(stdin)):
                    cli.main(str(self.home))
        output = stream.getvalue()
        return json.loads(output) if output.strip() else None

    def run_events_error(self, *argv, stdin=None):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events'] + [str(arg) for arg in argv]), \
                contextlib.redirect_stdout(stream):
            if stdin is None:
                with self.assertRaises(SystemExit) as caught:
                    cli.main(str(self.home))
            else:
                with patch('sys.stdin', io.StringIO(stdin)):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(str(self.home))
        return caught.exception

    def seed_message(self, mid, state='details_pending', chat_id='oc_test', sender_id='ou_user'):
        store = self.open_store()
        try:
            store.connection.execute(
                'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id) VALUES (?,?,?,?,?,?)',
                (mid, state, time.time(), time.time(), chat_id, sender_id))
        finally:
            store.close()

    def emit_message_event(self, mid, suffix=''):
        store = self.open_store()
        try:
            store.emit_event('message', 'message:%s%s' % (mid, suffix),
                             {'message_id': mid, 'chat_id': 'oc_test', 'text': 'hello'}, time.time())
        finally:
            store.close()

    def emit_attention_event(self, mid):
        store = self.open_store()
        try:
            store.emit_event('attention', 'attention:' + mid, {'message_id': mid}, time.time())
        finally:
            store.close()

    def test_register_creates_consumer(self):
        payload = self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['created'])
        self.assertEqual(payload['consumer']['name'], 'helper')
        self.assertEqual(payload['consumer']['kind'], 'dsh')
        again = self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        self.assertFalse(again['created'])

    def test_register_rejects_invalid_consumer_name(self):
        error = self.run_events_error('register', '--consumer', 'Bad Name', '--kind', 'dsh')
        self.assertIn('consumer name must match', str(error))

    def test_invalid_kind_is_rejected_by_argparse(self):
        error = self.run_events_error('register', '--consumer', 'helper', '--kind', 'bogus')
        self.assertEqual(error.code, 2)

    def test_claim_requires_registered_consumer(self):
        error = self.run_events_error('claim', '--consumer', 'ghost')
        self.assertIn('消费者未注册', str(error))
        self.assertIn('events register', str(error))

    def test_claim_on_empty_queue_returns_empty_list(self):
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        payload = self.run_events('claim', '--consumer', 'helper')
        self.assertEqual(payload['consumer'], 'helper')
        self.assertEqual(payload['events'], [])
        self.assertEqual(payload['count'], 0)

    def test_event_mutations_require_claim_token_argument(self):
        for command in ('ack', 'fail', 'reply'):
            with self.subTest(command=command):
                error = self.run_events_error(command, '--consumer', 'helper', '--event', 1,
                                              stdin='answer' if command == 'reply' else None)
                self.assertEqual(error.code, 2)

    def test_claim_exposes_token_for_consumer_submission(self):
        self.seed_message('om_token')
        self.emit_message_event('om_token')
        self.run_events('register', '--consumer', 'helper')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        self.assertIn('claim_token', event)
        self.assertIsInstance(event['claim_token'], str)
        self.assertTrue(event['claim_token'])

    def test_same_event_reply_retry_returns_durable_duplicate(self):
        self.seed_message('om_receipt')
        self.emit_message_event('om_receipt')
        self.run_events('register', '--consumer', 'helper')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        args = ('reply', '--consumer', 'helper', '--event', event['id'],
                '--claim-token', event['claim_token'])
        first = self.run_events(*args, stdin='durable answer')
        with patch('bridge.cli.time.time', return_value=event['lease_until'] + 10):
            repeated = self.run_events(*args, stdin='durable answer')
        self.assertFalse(first['duplicate'])
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(repeated['message_id'], 'om_receipt')
        store = self.open_store()
        try:
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
            self.assertEqual(store.get_outbox_by_inbox('om_receipt')['body'], render_reply('durable answer'))
        finally:
            store.close()

    # -- say / retract（S1d：出口唯一的两条新通道）--------------------------------
    def seed_sent_message(self, inbox_id, target_mid, body='已经发出去的那句'):
        """造一条「本桥已经发出去」的出站行：撤回只认 reply_id 对得上的消息。"""
        store = self.open_store()
        try:
            store.connection.execute(
                'INSERT INTO outbox(inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,'
                'created_at,next_due,state,reply_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (inbox_id, 'oc_test', 9, 'say', body,
                 'c' * 64, None, 'uuid-' + target_mid, time.time(), time.time(), 'sent', target_mid))
            store.connection.commit()
        finally:
            store.close()

    def claimed_message_event(self, mid='om_say', consumer='helper'):
        self.seed_message(mid)
        self.emit_message_event(mid)
        self.run_events('register', '--consumer', consumer)
        return self.run_events('claim', '--consumer', consumer)['events'][0]

    def outbox_rows(self):
        store = self.open_store()
        try:
            return [dict(row) for row in store.connection.execute(
                'SELECT * FROM outbox ORDER BY row_id').fetchall()]
        finally:
            store.close()

    def test_say_writes_say_row_without_touching_inbox(self):
        event = self.claimed_message_event('om_say')
        payload = self.run_events('say', '--consumer', 'helper', '--event', event['id'],
                                  '--claim-token', event['claim_token'],
                                  stdin='先看了一眼，马上好')
        self.assertTrue(payload['ok'])
        self.assertEqual((payload['kind'], payload['seq'], payload['duplicate']), ('say', 1, False))
        rows = self.outbox_rows()
        self.assertEqual([(row['kind'], row['state']) for row in rows], [('say', 'queued')])
        self.assertIn('先看了一眼', rows[0]['body'])
        self.assertEqual(rows[0]['body'], render_reply('先看了一眼，马上好'))  # 和终回复同一套署名
        store = self.open_store()
        try:
            self.assertEqual(store.connection.execute(
                'SELECT state FROM inbox WHERE id=?', ('om_say',)).fetchone()['state'],
                'details_pending')
            # say 不收尾事件：后面那句终回复还要用同一个 claim_token
            self.assertEqual(store.get_event(event['id'])['state'], 'leased')
        finally:
            store.close()

    def test_say_then_reply_keeps_write_order(self):
        event = self.claimed_message_event('om_ord')
        self.run_events('say', '--consumer', 'helper', '--event', event['id'],
                        '--claim-token', event['claim_token'], stdin='收到，我看看')
        reply = self.run_events('reply', '--consumer', 'helper', '--event', event['id'],
                                '--claim-token', event['claim_token'], stdin='做完了')
        self.assertEqual(reply['outbox_state'], 'queued')
        rows = self.outbox_rows()
        self.assertEqual([(row['seq'], row['kind']) for row in rows], [(1, 'say'), (2, 'reply')])

    def test_say_replay_with_same_uuid_writes_one_row(self):
        event = self.claimed_message_event('om_idem')
        args = ('say', '--consumer', 'helper', '--event', event['id'],
                '--claim-token', event['claim_token'], '--uuid', 'fixed-say-uuid')
        first = self.run_events(*args, stdin='同一句话')
        second = self.run_events(*args, stdin='同一句话')
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['row_id'], first['row_id'])
        self.assertEqual(len(self.outbox_rows()), 1)

    def test_say_rejects_mixed_or_missing_source(self):
        mixed = self.run_events_error('say', '--consumer', 'helper', '--event', 1,
                                      '--claim-token', 'token', '--chat', 'oc_x', stdin='x')
        self.assertIn('互斥', str(mixed))
        both_chat = self.run_events_error('say', '--consumer', 'helper', '--chat', 'oc_x',
                                          '--reply-to', 'om_x', stdin='x')
        self.assertIn('互斥', str(both_chat))
        nothing = self.run_events_error('say', '--consumer', 'helper', stdin='x')
        self.assertIn('三种来源之一', str(nothing))
        half = self.run_events_error('say', '--consumer', 'helper', '--event', 1, stdin='x')
        self.assertIn('--claim-token', str(half))

    def test_say_proactive_uses_chat_and_needs_registered_consumer(self):
        self.run_events('register', '--consumer', 'helper')
        payload = self.run_events('say', '--consumer', 'helper', '--chat', 'oc_proactive',
                                  stdin='主动说一句')
        self.assertTrue(payload['ok'])
        self.assertIsNone(payload['inbox_id'])
        self.assertEqual(payload['chat_id'], 'oc_proactive')
        rows = self.outbox_rows()
        self.assertEqual([(row['inbox_id'], row['chat_id'], row['kind']) for row in rows],
                         [(None, 'oc_proactive', 'say')])
        error = self.run_events_error('say', '--consumer', 'ghost', '--chat', 'oc_proactive',
                                      stdin='x')
        self.assertIn('消费者未注册', str(error))

    def test_say_reply_to_appends_after_existing_rows(self):
        self.seed_message('om_done', state='completed')
        store = self.open_store()
        try:
            store.append_outbox('om_done', 'reply', '原来的答案')
        finally:
            store.close()
        self.run_events('register', '--consumer', 'helper')
        payload = self.run_events('say', '--consumer', 'helper', '--reply-to', 'om_done',
                                  stdin='补一句：那台机器已经恢复')
        self.assertEqual((payload['kind'], payload['seq'], payload['inbox_id']),
                         ('say', 2, 'om_done'))
        rows = self.outbox_rows()
        self.assertEqual([(row['seq'], row['kind']) for row in rows], [(1, 'reply'), (2, 'say')])
        store = self.open_store()
        try:
            self.assertEqual(store.get_message('om_done')['state'], 'completed')  # 收尾状态不被翻回
        finally:
            store.close()

    def test_say_reply_to_unknown_message_is_refused_with_exit_4(self):
        self.run_events('register', '--consumer', 'helper')
        error = self.run_events_error('say', '--consumer', 'helper', '--reply-to', 'om_ghost',
                                      stdin='x')
        self.assertEqual(error.code, 4)

    def test_retract_target_outside_bridge_exits_4(self):
        event = self.claimed_message_event('om_retract')
        error = self.run_events_error('retract', '--consumer', 'helper', '--event', event['id'],
                                      '--claim-token', event['claim_token'],
                                      '--message-id', 'om_not_ours')
        self.assertEqual(error.code, 4)
        self.assertEqual(self.outbox_rows(), [])

    def test_retract_success_is_idempotent_and_refuses_second_retract(self):
        event = self.claimed_message_event('om_retract_ok')
        self.seed_sent_message('om_retract_ok', 'om_sent_by_bridge')
        args = ('retract', '--consumer', 'helper', '--event', event['id'],
                '--claim-token', event['claim_token'],
                '--message-id', 'om_sent_by_bridge')
        first = self.run_events(*args)
        self.assertTrue(first['ok'])
        replays = self.run_events(*args)  # 同一个默认 uuid：重放不多排一次
        self.assertTrue(replays['duplicate'])
        retracts = [row for row in self.outbox_rows() if row['kind'] == 'retract']
        self.assertEqual(len(retracts), 1)
        self.assertEqual(retracts[0]['target_mid'], 'om_sent_by_bridge')

        # 另一个 uuid 再撤同一条：拒绝（已经排过撤回），而不是再排一次 DELETE
        again = self.run_events_error('retract', '--consumer', 'helper', '--event', event['id'],
                                      '--claim-token', event['claim_token'],
                                      '--message-id', 'om_sent_by_bridge',
                                      '--uuid', 'another-retract-uuid')
        self.assertIn('already been retracted', str(again))

    def test_say_respects_daily_cap_but_retract_does_not(self):
        """配额挡住的是「再说一句」，不是「收回已经说出去的」。"""
        (self.home / 'config.json').write_text(json.dumps({'app_daily_reply_cap': 0}))
        store = self.open_store()
        try:
            store.connection.execute(
                'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id,sender_type)'
                ' VALUES (?,?,?,?,?,?,?)',
                ('om_app', 'external_queued', time.time(), time.time(), 'oc_app', 'ou_app', 'app'))
            store.connection.commit()
        finally:
            store.close()
        self.emit_message_event('om_app')
        self.run_events('register', '--consumer', 'helper')
        app_event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        blocked = self.run_events_error('say', '--consumer', 'helper', '--event', app_event['id'],
                                        '--claim-token', app_event['claim_token'], stdin='别刷屏')
        self.assertIn('reply-loop budget', str(blocked))

        # 用户消息一条都不许被配额牵连：撤回照常排队
        self.seed_message('om_human', chat_id='oc_human', sender_id='ou_human')
        self.emit_message_event('om_human')
        self.seed_sent_message('om_human', 'om_sent_by_bridge')
        human_event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        payload = self.run_events('retract', '--consumer', 'helper', '--event', human_event['id'],
                                  '--claim-token', human_event['claim_token'],
                                  '--message-id', 'om_sent_by_bridge')
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['kind'], 'retract')


        self.seed_message('om_reclaim')
        self.emit_message_event('om_reclaim')
        self.run_events('register', '--consumer', 'helper')
        with patch('bridge.cli.time.time', return_value=100):
            old = self.run_events('claim', '--consumer', 'helper', '--lease', 5)['events'][0]
        with patch('bridge.cli.time.time', return_value=106):
            new = self.run_events('claim', '--consumer', 'helper', '--lease', 20)['events'][0]
        with patch('bridge.cli.time.time', return_value=107):
            for command in ('reply', 'fail', 'ack'):
                with self.subTest(command=command):
                    self.run_events_error(command, '--consumer', 'helper', '--event', old['id'],
                                          '--claim-token', old['claim_token'],
                                          stdin='stale answer' if command == 'reply' else None)
            current = self.run_events('reply', '--consumer', 'helper', '--event', new['id'],
                                      '--claim-token', new['claim_token'], stdin='current answer')
        self.assertTrue(current['ok'])
        store = self.open_store()
        try:
            self.assertEqual(store.get_outbox_by_inbox('om_reclaim')['body'], render_reply('current answer'))
        finally:
            store.close()

    def test_message_ack_is_rejected_and_timeout_remains_recoverable(self):
        self.seed_message('om_not_replied', state='external_queued')
        self.emit_message_event('om_not_replied')
        self.run_events('register', '--consumer', 'helper')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        self.run_events_error('ack', '--consumer', 'helper', '--event', event['id'],
                              '--claim-token', event['claim_token'])
        store = self.open_store()
        try:
            self.assertEqual(store.get_event(event['id'])['state'], 'leased')
            self.assertEqual(store.expire_external(time.time() + 1000, 600), 1)
            self.assertEqual(store.get_message('om_not_replied')['state'], 'needs_attention')
        finally:
            store.close()

    def test_claim_respects_limit_and_lease(self):
        for mid in ('om_a', 'om_b', 'om_c'):
            self.seed_message(mid)
            self.emit_message_event(mid)
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        started = time.time()
        payload = self.run_events('claim', '--consumer', 'helper', '--limit', '2', '--lease', '60')
        self.assertEqual(payload['count'], 2)
        for event in payload['events']:
            self.assertEqual(event['kind'], 'message')
            self.assertEqual(event['attempts'], 1)
            self.assertGreater(event['lease_until'], started + 55)
            self.assertLess(event['lease_until'], time.time() + 61)
            self.assertEqual(event['payload']['message_id'], event['dedupe_key'].split(':', 1)[1])
        default_claim = self.run_events('claim', '--consumer', 'helper')
        self.assertEqual(default_claim['count'], 1)
        self.assertGreater(default_claim['events'][0]['lease_until'], started + 295)
        self.assertLess(default_claim['events'][0]['lease_until'], time.time() + 301)

    def test_claim_then_ack_marks_event_done(self):
        self.seed_message('om_done')
        self.emit_attention_event('om_done')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        claimed = self.run_events('claim', '--consumer', 'helper')
        event_id = claimed['events'][0]['id']
        token = claimed['events'][0]['claim_token']
        ack = self.run_events('ack', '--consumer', 'helper', '--event', event_id, '--claim-token', token)
        self.assertTrue(ack['ok'])
        listed = self.run_events('list', '--state', 'done')
        self.assertEqual([event['id'] for event in listed['events']], [event_id])
        self.assertEqual(listed['events'][0]['state'], 'done')
        self.assertEqual(self.run_events('claim', '--consumer', 'helper')['events'], [])

    def test_ack_of_unleased_event_exits_nonzero(self):
        self.seed_message('om_ack')
        self.emit_attention_event('om_ack')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        self.assertTrue(self.run_events('ack', '--consumer', 'helper', '--event', event_id, '--claim-token', token)['ok'])
        error = self.run_events_error('ack', '--consumer', 'helper', '--event', event_id, '--claim-token', token)
        self.assertIn('ack 未生效', str(error))

    def test_fail_without_permanent_returns_event_to_pending(self):
        self.seed_message('om_retry')
        self.emit_message_event('om_retry')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        payload = self.run_events('fail', '--consumer', 'helper', '--event', event_id, '--claim-token', token,
                                  '--error', 'model timeout', '--retry-delay', '0')
        self.assertEqual(payload['state'], 'pending')
        again = self.run_events('claim', '--consumer', 'helper')
        self.assertEqual(again['count'], 1)
        self.assertEqual(again['events'][0]['attempts'], 2)

    def test_fail_permanent_marks_dead_and_flags_inbox(self):
        self.seed_message('om_dead')
        self.emit_message_event('om_dead')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        payload = self.run_events('fail', '--consumer', 'helper', '--event', event_id, '--claim-token', token,
                                  '--error', 'unrecoverable', '--permanent')
        self.assertEqual(payload['state'], 'dead')
        store = self.open_store()
        try:
            self.assertEqual(store.get_message('om_dead')['state'], 'needs_attention')
            self.assertEqual(store.event_stats(time.time())['states'].get('dead'), 1)
        finally:
            store.close()

    def test_reply_from_stdin_queues_outbox_and_is_idempotent(self):
        self.seed_message('om_reply')
        self.emit_message_event('om_reply')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        payload = self.run_events('reply', '--consumer', 'helper', '--event', event_id, '--claim-token', token,
                                  stdin='这是答案 697\n')
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['duplicate'])
        self.assertEqual(payload['outbox_state'], 'queued')
        self.assertEqual(payload['message_id'], 'om_reply')
        store = self.open_store()
        try:
            outbox = store.get_outbox_by_inbox('om_reply')
            self.assertEqual(outbox['state'], 'queued')
            self.assertEqual(outbox['body'], render_reply('这是答案 697'))
            self.assertEqual(outbox['uuid'], reply_uuid('om_reply'))
            self.assertEqual(store.get_message('om_reply')['state'], 'reply_queued')
        finally:
            store.close()
        # 同一 message 的第二个事件（不同 dedupe_key）再回复：幂等，不重发、不覆盖正文
        self.emit_message_event('om_reply', ':retry')
        second = self.run_events('claim', '--consumer', 'helper')['events'][0]
        second_id = second['id']
        duplicate = self.run_events('reply', '--consumer', 'helper', '--event', second_id, '--claim-token', second['claim_token'],
                                    stdin='这是答案 697\n')
        self.assertTrue(duplicate['duplicate'])
        store = self.open_store()
        try:
            self.assertEqual(store.get_outbox_by_inbox('om_reply')['body'], render_reply('这是答案 697'))
        finally:
            store.close()

    def test_skip_closes_the_event_without_sending_anything(self):
        self.seed_message('om_skip')
        self.emit_message_event('om_skip')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        payload = self.run_events('skip', '--consumer', 'helper', '--event', event['id'],
                                  '--claim-token', event['claim_token'],
                                  '--reason', '对方只是回了一句哦哦')
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['duplicate'])
        self.assertEqual(payload['message_id'], 'om_skip')
        store = self.open_store()
        try:
            self.assertIsNone(store.get_outbox_by_inbox('om_skip'))
            self.assertEqual(store.get_message('om_skip')['state'], 'ignored')
            self.assertIn('对方只是回了一句哦哦', store.get_message('om_skip')['error'])
            self.assertEqual(store.event_stats(time.time())['states'].get('done'), 1)
        finally:
            store.close()

    def test_skip_requires_a_non_empty_reason(self):
        self.seed_message('om_skip2')
        self.emit_message_event('om_skip2')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        caught = self.run_events_error('skip', '--consumer', 'helper', '--event', event['id'],
                                       '--claim-token', event['claim_token'], '--reason', '  ')
        self.assertIn('--reason', str(caught.code))

    def seed_group_event(self, mid, sender_id='ou_peer'):
        store = self.open_store()
        try:
            store.emit_event('message', 'message:' + mid,
                             {'message_id': mid, 'chat_id': 'oc_group', 'sender_id': sender_id,
                              'text': '@_user_1 哦哦', 'chat_type': 'group'}, time.time())
        finally:
            store.close()

    def test_group_reply_mentions_the_asker_not_the_owner(self):
        # 配置里记着本机账号；回复正文里出现指向它的 @ 必须被删掉。
        (self.home / 'config.json').write_text(json.dumps(
            {'target_open_id': 'ou_owner', 'signature': '署名'}))
        self.seed_message('om_ask')
        self.seed_group_event('om_ask')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        self.run_events('reply', '--consumer', 'helper', '--event', event['id'],
                        '--claim-token', event['claim_token'],
                        stdin='<at user_id="ou_owner"></at> 收到 @_user_1\n')
        store = self.open_store()
        try:
            body = store.get_outbox_by_inbox('om_ask')['body']
        finally:
            store.close()
        self.assertTrue(body.startswith('<at user_id="ou_peer"></at> '), body)
        self.assertNotIn('ou_owner', body)
        self.assertNotIn('@_user_1', body)

    def test_private_reply_has_no_mention(self):
        self.seed_message('om_dm')
        store = self.open_store()
        try:
            store.emit_event('message', 'message:om_dm',
                             {'message_id': 'om_dm', 'chat_id': 'oc_dm', 'sender_id': 'ou_owner',
                              'text': '你好', 'chat_type': 'p2p'}, time.time())
        finally:
            store.close()
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        self.run_events('reply', '--consumer', 'helper', '--event', event['id'],
                        '--claim-token', event['claim_token'], stdin='好的\n')
        store = self.open_store()
        try:
            body = store.get_outbox_by_inbox('om_dm')['body']
        finally:
            store.close()
        self.assertNotIn('<at', body)

    def test_reply_is_refused_while_held(self):
        self.seed_message('om_held')
        self.emit_message_event('om_held')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        (self.home / 'hold.json').write_text(json.dumps({'active': True, 'since_ms': 1}))
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events', 'reply', '--consumer', 'helper',
                                '--event', str(event['id']), '--claim-token', event['claim_token']]), \
                patch('sys.stdin', io.StringIO('挂起期间的回复\n')), \
                contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertIn('挂起', str(caught.exception))
        store = self.open_store()
        try:
            self.assertIsNone(store.get_outbox_by_inbox('om_held'))
        finally:
            store.close()

    def test_reply_reads_body_file(self):
        self.seed_message('om_file')
        self.emit_message_event('om_file')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        body_path = self.home / 'reply.txt'
        body_path.write_text('文件正文')
        payload = self.run_events('reply', '--consumer', 'helper', '--event', event_id, '--claim-token', token,
                                  '--body-file', str(body_path))
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['duplicate'])
        store = self.open_store()
        try:
            self.assertEqual(store.get_outbox_by_inbox('om_file')['body'], render_reply('文件正文'))
        finally:
            store.close()

    def test_reply_never_accepts_argv_body(self):
        error = self.run_events_error('reply', '--consumer', 'helper', '--event', '1', '--claim-token', 'test-token', '正文在 argv')
        self.assertEqual(error.code, 2)

    def test_reply_empty_stdin_is_rejected(self):
        error = self.run_events_error('reply', '--consumer', 'helper', '--event', '1', '--claim-token', 'test-token', stdin='   \n')
        self.assertIn('正文为空', str(error))

    def test_reply_body_file_with_invalid_utf8_is_rejected(self):
        body_path = self.home / 'bad.bin'
        body_path.write_bytes(b'\xff\xfe\x00')
        error = self.run_events_error('reply', '--consumer', 'helper', '--event', '1', '--claim-token', 'test-token',
                                      '--body-file', str(body_path))
        self.assertIn('无法读取', str(error))

    def test_fail_rejects_negative_retry_delay(self):
        self.seed_message('om_neg')
        self.emit_message_event('om_neg')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        error = self.run_events_error('fail', '--consumer', 'helper', '--event', event_id, '--claim-token', token,
                                      '--retry-delay', '-5')
        self.assertIn('--retry-delay', str(error))

    def test_reply_on_attention_event_is_rejected(self):
        store = self.open_store()
        try:
            store.emit_event('attention', 'attention:om_x',
                             {'message_id': 'om_x', 'error': 'needs a human', 'state': 'needs_attention'},
                             time.time())
        finally:
            store.close()
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper', '--kind', 'attention')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        error = self.run_events_error('reply', '--consumer', 'helper', '--event', event_id, '--claim-token', token, stdin='answer')
        self.assertIn('不能 reply', str(error))

    def test_reply_on_unleased_event_is_rejected(self):
        self.seed_message('om_lease')
        self.emit_message_event('om_lease')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        event = self.run_events('claim', '--consumer', 'helper')['events'][0]
        event_id = event['id']
        token = event['claim_token']
        self.run_events('fail', '--consumer', 'helper', '--event', event_id, '--claim-token', token)
        error = self.run_events_error('reply', '--consumer', 'helper', '--event', event_id, '--claim-token', token, stdin='answer')
        self.assertIn('reply 被拒绝', str(error))

    def test_attention_events_can_be_claimed_and_acked(self):
        store = self.open_store()
        try:
            store.emit_event('attention', 'attention:om_att',
                             {'message_id': 'om_att', 'error': 'e', 'state': 'needs_attention'}, time.time())
        finally:
            store.close()
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        claimed = self.run_events('claim', '--consumer', 'helper', '--kind', 'attention')
        self.assertEqual(claimed['count'], 1)
        self.assertEqual(claimed['events'][0]['kind'], 'attention')
        ack = self.run_events('ack', '--consumer', 'helper', '--event', claimed['events'][0]['id'],
                              '--claim-token', claimed['events'][0]['claim_token'])
        self.assertTrue(ack['ok'])

    def test_timeout_writes_external_config_atomically(self):
        result = self.run_events('timeout', '900')
        self.assertEqual(result['reply_timeout_seconds'], 900)
        path = self.home / 'external.json'
        self.assertEqual(json.loads(path.read_text()), {'reply_timeout_seconds': 900})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([item.name for item in self.home.iterdir() if item.name.endswith('.tmp')], [])
    def test_timeout_rejects_out_of_range_values(self):
        error = self.run_events_error('timeout', '5')
        self.assertIn('秒之间', str(error))
        self.assertFalse((self.home / 'external.json').exists())
    def test_status_reports_consumer_and_queue_summary(self):
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        report = self.run_events('status')
        self.assertEqual(report['external']['reply_timeout_seconds'], 600)
        self.assertTrue(report['db_present'])
        self.assertEqual(report['events']['states'], {})
        self.assertEqual([consumer['name'] for consumer in report['consumers']], ['helper'])
        self.assertEqual(set(('external', 'events', 'consumers')) - set(report), set())
    def test_status_without_database_still_reports_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            stream = io.StringIO()
            with patch('sys.argv', ['feishu-bridge', 'events', 'status']), contextlib.redirect_stdout(stream):
                cli.main(str(home))
            report = json.loads(stream.getvalue())
        self.assertFalse(report['db_present'])
        self.assertEqual(report['events'], {})
        self.assertEqual(report['consumers'], [])
        self.assertEqual(report['external']['reply_timeout_seconds'], 600)
        self.assertIn('state.sqlite3', report['error'])

    def test_events_require_initialized_database(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch('sys.argv', ['feishu-bridge', 'events', 'claim', '--consumer', 'helper']):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(str(home))
        self.assertIn('state.sqlite3', str(caught.exception))

    def test_list_filters_by_state_and_consumer(self):
        self.seed_message('om_one')
        self.emit_attention_event('om_one')
        self.seed_message('om_two')
        self.emit_attention_event('om_two')
        self.run_events('register', '--consumer', 'helper', '--kind', 'dsh')
        claimed = self.run_events('claim', '--consumer', 'helper', '--limit', '1')
        acked_id = claimed['events'][0]['id']
        token = claimed['events'][0]['claim_token']
        self.run_events('ack', '--consumer', 'helper', '--event', acked_id, '--claim-token', token)
        pending = self.run_events('list', '--state', 'pending')
        self.assertEqual(pending['count'], 1)
        self.assertEqual(pending['events'][0]['state'], 'pending')
        mine = self.run_events('list', '--consumer', 'helper')
        self.assertEqual([event['id'] for event in mine['events']], [acked_id])
        error = self.run_events_error('list', '--state', 'bogus')
        self.assertIn('未知事件状态', str(error))

    def test_stats_reports_event_counts(self):
        self.seed_message('om_stat')
        self.emit_message_event('om_stat')
        stats = self.run_events('stats')
        self.assertEqual(stats['states'], {'pending': 1})
        self.assertEqual(stats['kinds'], {'message': 1})
        self.assertEqual(stats['external_queued'], 0)

    def test_events_prune_dry_run_then_apply(self):
        self.open_store().close()
        store = self.open_store()
        now = 100000.0
        with store._transaction() as db:
            db.execute("""INSERT INTO events(kind,dedupe_key,payload,created_at,state)
                          VALUES('message','message:old','{}',?,'dead')""", (now - 30 * 86400,))
        store.close()
        dry = self.run_events('prune', '--dry-run')
        self.assertTrue(dry['dry_run'])
        self.assertEqual(dry['events_dead_deleted'], 1)
        self.assertEqual(self.run_events('stats')['states'], {'dead': 1})
        applied = self.run_events('prune')
        self.assertEqual(applied['events_dead_deleted'], 1)
        self.assertEqual(self.run_events('stats')['states'], {})

    def test_events_help_uses_events_parser(self):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events', '--help']), contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('feishu-bridge events', stream.getvalue())
        self.assertIn('register', stream.getvalue())

    def test_events_subcommand_help_shows_lease_default(self):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events', 'claim', '--help']), contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('--lease', stream.getvalue())
        self.assertIn('300', stream.getvalue())

    def test_home_flag_inside_events_is_supported(self):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events', '--home', str(self.home), 'status']), \
                contextlib.redirect_stdout(stream):
            cli.main(None)
        report = json.loads(stream.getvalue())
        self.assertTrue(report['db_present'])
        self.assertEqual(report['database'], str(self.home.resolve() / 'state.sqlite3'))

    def test_home_flag_after_legacy_command_still_works(self):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'status', '--home', str(self.home)]), \
                patch.object(cli, 'launch_status', return_value={'loaded': False}), \
                contextlib.redirect_stdout(stream):
            cli.main(None)
        self.assertEqual(json.loads(stream.getvalue())['home'], str(self.home.resolve()))

    def test_home_flag_can_precede_events(self):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', '--home', str(self.home), 'events', 'status']), \
                contextlib.redirect_stdout(stream):
            cli.main(None)
        self.assertTrue(json.loads(stream.getvalue())['db_present'])

    def test_unknown_argument_for_legacy_command_still_fails(self):
        with patch('sys.argv', ['feishu-bridge', 'status', '--bogus']):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertEqual(caught.exception.code, 2)

    def test_events_without_subcommand_fails(self):
        with patch('sys.argv', ['feishu-bridge', 'events']):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertEqual(caught.exception.code, 2)


    def test_token_command_creates_and_rotates_private_file(self):
        first = self.run_events('token')
        token = first['token']
        self.assertEqual(len(token), 48)
        self.assertTrue(first['created'])
        path = Path(first['path'])
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_events('token')['token'], token)
        rotated = self.run_events('token', '--rotate')
        self.assertNotEqual(rotated['token'], token)
        self.assertEqual(gateway.read_token(path), rotated['token'])

    def test_serve_requires_existing_token(self):
        error = self.run_events_error('serve')
        self.assertIn('events token', str(error))

    def test_serve_refuses_non_loopback_without_allow_remote(self):
        self.run_events('token')
        error = self.run_events_error('serve', '--host', '0.0.0.0', '--port', '18766')
        self.assertIn('--allow-remote', str(error))

    def test_serve_help_lists_remote_flag(self):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'events', 'serve', '--help']), \
                contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('--allow-remote', stream.getvalue())
        self.assertIn('--port', stream.getvalue())




class TaskCommandTests(unittest.TestCase):
    """`feishu-bridge task ...` 与 `events reply --task-*`：账本对外的唯一读写口。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)
        store = Store(str(self.home / 'state.sqlite3'))
        store.close()

    def tearDown(self):
        self._directory.cleanup()

    def open_store(self):
        return Store(str(self.home / 'state.sqlite3'))

    def run_cli(self, *argv, stdin=None, command='task'):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', command] + [str(arg) for arg in argv]), \
                contextlib.redirect_stdout(stream):
            if stdin is None:
                code = cli.main(str(self.home))
            else:
                with patch('sys.stdin', io.StringIO(stdin)):
                    code = cli.main(str(self.home))
        output = stream.getvalue()
        return code, (json.loads(output) if output.strip() else None)

    def run_error(self, *argv, command='task'):
        with patch('sys.argv', ['feishu-bridge', command] + [str(arg) for arg in argv]):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        return caught.exception

    def seed_task(self, title='把三张表汇总'):
        store = self.open_store()
        try:
            return store.draft_task('oc_test', title, time.time())
        finally:
            store.close()

    def test_list_and_show_report_the_ledger_as_json(self):
        task = self.seed_task()
        code, listing = self.run_cli('list')
        self.assertEqual(code, 0)
        self.assertEqual(listing['count'], 1)
        self.assertEqual(listing['tasks'][0]['id'], task['id'])
        self.assertEqual(listing['tasks'][0]['state'], 'open')
        code, shown = self.run_cli('show', task['id'])
        self.assertEqual(code, 0)
        self.assertEqual((shown['id'], shown['steps'], shown['artifacts']),
                         (task['id'], [], []))

    def test_list_filters_by_state_and_rejects_unknown_states(self):
        task = self.seed_task()
        store = self.open_store()
        try:
            store.set_task_state(task['id'], 'done', time.time())
        finally:
            store.close()
        self.assertEqual(self.run_cli('list', '--state', 'open')[1]['count'], 0)
        self.assertEqual(self.run_cli('list', '--state', 'done')[1]['count'], 1)
        self.assertEqual(self.run_error('list', '--state', 'nonsense').code, 2)

    def test_cancel_is_the_only_state_write_and_keeps_history(self):
        task = self.seed_task()
        _, cancelled = self.run_cli('cancel', task['id'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(cancelled['title'], '把三张表汇总')

    def test_show_reports_an_unknown_task_instead_of_an_empty_ledger(self):
        error = self.run_error('show', 'tsk_missing')
        self.assertIn('没有这个任务', str(error))

    def test_task_commands_fail_loudly_without_a_database(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('sys.argv', ['feishu-bridge', 'task', 'list']):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(temp)
        self.assertIn('未找到状态库', str(caught.exception))

    def test_reply_flags_write_the_step_into_the_ledger(self):
        task = self.seed_task()
        store = self.open_store()
        try:
            store.connection.execute(
                'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id) VALUES (?,?,?,?,?,?)',
                ('om_step', 'external_queued', time.time(), time.time(), 'oc_test', 'ou_user'))
            store.connection.execute('INSERT INTO task_messages(message_id,task_id) VALUES (?,?)',
                                     ('om_step', task['id']))
            store.emit_event('message', 'message:om_step',
                             {'message_id': 'om_step', 'chat_id': 'oc_test', 'text': 'hello'}, time.time())
        finally:
            store.close()
        self.run_cli('register', '--consumer', 'helper', command='events')
        _, claimed = self.run_cli('claim', '--consumer', 'helper', command='events')
        event = claimed['events'][0]
        _, receipt = self.run_cli(
            'reply', '--consumer', 'helper', '--event', event['id'],
            '--claim-token', event['claim_token'],
            '--task-state', 'running', '--task-note', '读完 2/3',
            '--task-artifacts-json', '[{"path": "/tmp/out.xlsx", "kind": "file"}]',
            stdin='第一张和第二张读完了', command='events')
        self.assertTrue(receipt['ok'])
        code, shown = self.run_cli('show', task['id'])
        self.assertEqual(code, 0)
        self.assertEqual(shown['state'], 'running')
        self.assertEqual(shown['steps'][0]['outcome'], 'sent')
        self.assertEqual(shown['steps'][0]['note'], '读完 2/3')
        self.assertEqual([item['path'] for item in shown['artifacts']], ['/tmp/out.xlsx'])

    def test_reply_without_task_flags_keeps_the_old_contract(self):
        store = self.open_store()
        try:
            store.connection.execute(
                'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id) VALUES (?,?,?,?,?,?)',
                ('om_plain', 'external_queued', time.time(), time.time(), 'oc_test', 'ou_user'))
            store.emit_event('message', 'message:om_plain',
                             {'message_id': 'om_plain', 'chat_id': 'oc_test', 'text': 'hello'}, time.time())
        finally:
            store.close()
        self.run_cli('register', '--consumer', 'helper', command='events')
        _, claimed = self.run_cli('claim', '--consumer', 'helper', command='events')
        event = claimed['events'][0]
        _, receipt = self.run_cli('reply', '--consumer', 'helper', '--event', event['id'],
                                  '--claim-token', event['claim_token'],
                                  stdin='一个不带账本的回复', command='events')
        self.assertTrue(receipt['ok'])
        code, listing = self.run_cli('list')
        self.assertEqual(code, 0)
        self.assertEqual(listing['count'], 0)

    def test_malformed_artifact_json_is_rejected_before_any_write(self):
        error = self.run_error('reply', '--consumer', 'helper', '--event', 1,
                               '--claim-token', 'x' * 32,
                               '--task-artifacts-json', 'not json', command='events')
        self.assertIn('合法 JSON', str(error))


class RemindCommandTests(unittest.TestCase):
    """`feishu-bridge remind ...`：登记/查看/取消。到点发送由服务心跳负责（见 test_service）。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'remind'] + list(argv)):
            with redirect_stdout(buffer):
                cli.main(str(self.home))
        output = buffer.getvalue()
        try:
            return json.loads(output)
        except ValueError:
            return output

    def test_a_reminder_lands_in_the_same_database_the_service_reads(self):
        # 真机踩过：CLI 写 bridge.db、服务读 state.sqlite3，"设上了"却到点一声没响。
        added = self.run_cli('add', '--at', '21:40', '--text', '给张三打电话',
                             '--chat', 'oc_owner', '--message-id', 'om_9')
        self.assertTrue(added['ok'])
        self.assertEqual(added['fire_at'][-5:], '21:40')
        database = self.home / 'state.sqlite3'
        self.assertTrue(database.exists())
        self.assertFalse((self.home / 'bridge.db').exists())
        store = Store(database)
        try:
            rows = store.reminders()
        finally:
            store.close()
        self.assertEqual([row['text'] for row in rows], ['给张三打电话'])

    def test_the_same_source_message_cannot_register_two_reminders(self):
        first = self.run_cli('add', '--at', '21:40', '--text', '给张三打电话',
                             '--chat', 'oc_owner', '--message-id', 'om_9')
        again = self.run_cli('add', '--at', '22:40', '--text', '别的',
                             '--chat', 'oc_owner', '--message-id', 'om_9')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(again['text'], '给张三打电话')

    def test_a_reminder_can_be_cancelled_by_name_and_then_disappears(self):
        self.run_cli('add', '--at', '21:40', '--text', '给张三打电话', '--chat', 'oc_owner',
                     '--message-id', 'om_9')
        cancelled = self.run_cli('cancel', '给张三打电话')
        self.assertTrue(cancelled['ok'])
        store = Store(self.home / 'state.sqlite3')
        try:
            self.assertEqual(store.reminders(), [])
            self.assertEqual(store.due_reminders(10 ** 13), [])
        finally:
            store.close()

    def test_an_impossible_date_is_reported_instead_of_silently_rolled_over(self):
        payload = self.run_cli('add', '--at', '2026-02-30 09:00', '--text', '给张三打电话',
                               '--chat', 'oc_owner', '--message-id', 'om_9')
        self.assertFalse(payload['ok'])
        self.assertIn('没有这一天', payload['reason'])

    def test_status_reports_the_failures_that_happened_last_not_the_latest_fire_time(self):
        store = Store(self.home / 'state.sqlite3')
        try:
            now_ms = 10 ** 12
            # 两条提醒：A 原定更晚、但先失败；B 原定更早、后失败。
            # 按"到点时刻"排序会得出 B、A（错），按失败发生的时刻才是 A、B。
            old_fail, _ = store.add_reminder(now_ms + 900000, '先失败的', '先失败的', 'oc_owner',
                                             'om_a', now_ms)
            new_fail, _ = store.add_reminder(now_ms + 800000, '后失败的', '后失败的', 'oc_owner',
                                             'om_b', now_ms)
            store.finish_reminder(old_fail['id'], 'failed', fired_at_ms=now_ms + 100, error='400')
            store.finish_reminder(new_fail['id'], 'failed', fired_at_ms=now_ms + 200, error='429')
        finally:
            store.close()
        payload = self.run_cli('status')
        self.assertEqual([row['name'] for row in payload['recent_failed']],
                         ['先失败的', '后失败的'])

    def test_list_shows_the_name_and_the_relative_time(self):
        self.run_cli('add', '--at', '21:40', '--text', '给张三打电话', '--chat', 'oc_owner',
                     '--message-id', 'om_9')
        output = self.run_cli('list')
        self.assertIn('待发提醒 1 条', output)
        self.assertIn('「给张三打电话」', output)


class LinuxLifecycleTests(unittest.TestCase):
    """systemd --user lifecycle is the WSL2 counterpart of the launchd path."""

    def test_start_runs_daemon_reload_enable_start(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / 'feishu-bridge.service'
            unit.write_text('[Service]\nExecStart=/tmp/feishu-bridge foreground\n')
            calls = []

            def fake(args, timeout=60, scope='user'):
                calls.append([list(args), scope])
                return FakeResult()

            with patch.object(cli.host, 'SYSTEM', 'linux'), \
                    patch.object(cli, '_run_systemctl', side_effect=fake), \
                    patch.object(cli, 'systemd_status',
                                 return_value={'loaded': True, 'active': True, 'state': 'running'}):
                cli._lifecycle_systemd('start', unit)
        self.assertEqual([entry[0] for entry in calls],
                         [['daemon-reload'], ['enable', 'feishu-bridge.service'],
                          ['start', 'feishu-bridge.service']])
        self.assertEqual({entry[1] for entry in calls}, {'user'})

    def test_missing_unit_exits_with_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as caught:
                cli._lifecycle_systemd('start', Path(directory) / 'missing.service')
        self.assertIn('install.py', str(caught.exception))

    def test_restart_stops_then_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / 'feishu-bridge.service'
            unit.write_text('[Service]\nExecStart=/tmp/feishu-bridge foreground\n')
            calls = []

            def fake(args, timeout=60, scope='user'):
                calls.append([list(args), scope])
                return FakeResult()

            with patch.object(cli.host, 'SYSTEM', 'linux'), \
                    patch.object(cli, '_run_systemctl', side_effect=fake), \
                    patch.object(cli, 'systemd_status',
                                 return_value={'loaded': True, 'active': True, 'state': 'running'}):
                cli._lifecycle_systemd('restart', unit)
        self.assertEqual([entry[0][0] for entry in calls], ['stop', 'daemon-reload', 'enable', 'restart'])

    def test_status_uses_systemd_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'status.json').write_text(json.dumps({'pid': 7, 'heartbeat_at': time.time(),
                                                          'authenticated': True,
                                                          'last_search_at': time.time()}))
            stream = io.StringIO()
            with patch.object(cli.host, 'SYSTEM', 'linux'), \
                    patch.object(cli, 'systemd_status',
                                 return_value={'loaded': True, 'active': True, 'state': 'running', 'pid': 7}), \
                    contextlib.redirect_stdout(stream):
                cli._print_status(home)
            data = json.loads(stream.getvalue())
        self.assertEqual(data['service_backend'], 'systemd')
        self.assertTrue(data['process_running'])
        self.assertTrue(data['systemd']['active'])


if __name__ == '__main__':
    unittest.main()


class SystemdScopeTests(unittest.TestCase):
    """WSL2 会回收用户级 systemd：装在系统级的部署也必须被 status/lifecycle 认出来。"""

    def test_status_falls_back_to_system_scope_when_user_inactive(self):
        user = {'loaded': True, 'unit': 'u.service', 'scope': 'user', 'active': False,
                'state': 'dead', 'pid': None}
        system = {'loaded': True, 'unit': 'u.service', 'scope': 'system', 'active': True,
                  'state': 'running', 'pid': 42}
        with patch.object(cli, '_systemd_show', side_effect=[user, system]):
            status = cli.systemd_status('u.service')
        self.assertEqual(status['scope'], 'system')
        self.assertEqual(status['pid'], 42)

    def test_status_prefers_user_scope_when_it_is_active(self):
        user = {'loaded': True, 'unit': 'u.service', 'scope': 'user', 'active': True,
                'state': 'running', 'pid': 7}
        with patch.object(cli, '_systemd_show', side_effect=[user]) as show:
            status = cli.systemd_status('u.service')
        self.assertEqual(status['scope'], 'user')
        self.assertEqual(show.call_count, 1)

    def test_lifecycle_targets_system_scope_when_installed_there(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / 'u.service'
            unit.write_text('[Service]\n')
            calls = []

            def fake(args, timeout=60, scope='user'):
                calls.append([list(args), scope])
                return FakeResult()

            with patch.object(cli, '_system_scope_installed', return_value=True), \
                    patch.object(cli, '_run_systemctl', side_effect=fake), \
                    patch.object(cli, 'systemd_status',
                                 return_value={'loaded': True, 'active': True, 'state': 'running'}):
                cli._lifecycle_systemd('restart', unit)
        self.assertEqual({entry[1] for entry in calls}, {'system'})
        self.assertEqual([entry[0][0] for entry in calls], ['stop', 'daemon-reload', 'enable', 'restart'])


class TeachTransportFactoryTests(unittest.TestCase):
    """真实 `_teach_transport` 必须能跑起来：别的测试都把它替换掉了，
    结果 `threading` 漏 import 这种错误只在真机上才炸（2026-11-05 实测踩到）。"""

    def test_factory_builds_a_transport(self):
        from unittest.mock import patch
        from bridge import cli

        config = {'target_open_id': 'ou_owner', 'sending_enabled': True}
        with patch('bridge.transport.Transport') as fake:
            built = cli._teach_transport(config)
        fake.assert_called_once()
        self.assertIs(built, fake.return_value)


class TeachCommandTests(unittest.TestCase):
    """`feishu-bridge teach ...`：终端侧的查看/撤回/启用；串行化由 teach.lock 负责。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)
        from bridge import teach as teach_log
        self.teach = teach_log
        (self.home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x'}))

    def tearDown(self):
        self._directory.cleanup()

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'teach'] + list(argv)):
            with redirect_stdout(buffer):
                cli.main(str(self.home))
        return buffer.getvalue()

    def add(self, text='引用文件时给绝对路径', mid='om_1', kind='principle'):
        record = self.teach.new_entry(kind, text, mid, 1793790000000)
        return self.teach.append(self.home, record)

    def test_enable_rejects_group_chat_and_writes_nothing(self):
        fake = type('Fake', (), {'chat_info': lambda self, chat_id: {'chat_mode': 'group'}})()
        with patch.object(cli, '_teach_transport', return_value=fake):
            with self.assertRaises(SystemExit) as caught:
                self.run_cli('enable', '--chat', 'oc_group')
        self.assertIn('不是私聊', str(caught.exception))
        self.assertFalse((self.home / 'teach.json').exists())

    def test_status_explains_why_the_default_channel_is_not_collecting(self):
        # 默认开通：没配置时要说清是"缺权限"还是"还没认出来"，不能只给一句 enabled:false。
        fake = type('Fake', (), {'user_scopes': lambda self: {'im:message'}})()
        with patch.object(cli, '_teach_transport', return_value=fake):
            output = self.run_cli('status')
        status = json.loads(output)
        self.assertFalse(status['enabled'])
        self.assertTrue(status['default_on'])
        self.assertEqual(status['reason'], 'no_p2p_scope')

    def test_enable_refuses_private_chat_without_the_p2p_read_scope(self):
        fake = type('Fake', (), {'chat_info': lambda self, chat_id: {'chat_mode': 'p2p'},
                                 'user_scopes': lambda self: {'im:message', 'search:message'}})()
        with patch.object(cli, '_teach_transport', return_value=fake):
            with self.assertRaises(SystemExit) as caught:
                self.run_cli('enable', '--chat', 'oc_self')
        self.assertIn('im:message.p2p_msg:get_as_user', str(caught.exception))
        self.assertIn('lark-cli auth login --scope', str(caught.exception))
        self.assertFalse((self.home / 'teach.json').exists())

    def test_enable_writes_config_with_enabled_at_ms(self):
        fake = type('Fake', (), {'chat_info': lambda self, chat_id: {'chat_mode': 'p2p'},
                                 'user_scopes': lambda self: {'im:message.p2p_msg:get_as_user'}})()
        before = int(__import__('time').time() * 1000)
        with patch.object(cli, '_teach_transport', return_value=fake):
            output = self.run_cli('enable', '--chat', 'oc_self')
        config = json.loads((self.home / 'teach.json').read_text())
        self.assertTrue(config['enabled'])
        self.assertEqual(config['chat_id'], 'oc_self')
        self.assertGreaterEqual(config['enabled_at_ms'], before)
        self.assertEqual(config['max_entries'], self.teach.MAX_ENTRIES)
        self.assertIn('已启用教学', output)

    def test_enable_self_recognises_the_chat_where_the_other_side_is_me(self):
        # 私聊要显式登记才被采集；但"自己去飞书里翻 oc_…"是这道门槛上唯一的摩擦，
        # 而这个会话是可算出来的（p2p 且对方就是本人）。
        fake = type('Fake', (), {'self_chat_id': lambda self: 'oc_selfchat',
                                 'chat_info': lambda self, chat_id: {'chat_mode': 'p2p'},
                                 'user_scopes': lambda self: {'im:message.p2p_msg:get_as_user'}})()
        with patch.object(cli, '_teach_transport', return_value=fake):
            output = self.run_cli('enable', '--self')
        self.assertIn('自动认出的本人会话：oc_selfchat', output)
        self.assertEqual(json.loads((self.home / 'teach.json').read_text())['chat_id'], 'oc_selfchat')

    def test_enable_needs_exactly_one_of_chat_or_self(self):
        fake = type('Fake', (), {'self_chat_id': lambda self: 'oc_selfchat',
                                 'chat_info': lambda self, chat_id: {'chat_mode': 'p2p'},
                                 'user_scopes': lambda self: {'im:message.p2p_msg:get_as_user'}})()
        with patch.object(cli, '_teach_transport', return_value=fake):
            for argv in (('enable',), ('enable', '--self', '--chat', 'oc_self')):
                with self.subTest(argv=argv):
                    with self.assertRaises(SystemExit) as caught:
                        self.run_cli(*argv)
                    self.assertIn('二选一', str(caught.exception))
        self.assertFalse((self.home / 'teach.json').exists())

    def test_enable_self_refuses_to_guess_when_it_cannot_recognise_it(self):
        fake = type('Fake', (), {
            'self_chat_id': lambda self: (_ for _ in ()).throw(RuntimeError('没找到')),
            'chat_info': lambda self, chat_id: {'chat_mode': 'p2p'},
            'user_scopes': lambda self: {'im:message.p2p_msg:get_as_user'}})()
        with patch.object(cli, '_teach_transport', return_value=fake):
            with self.assertRaises(SystemExit) as caught:
                self.run_cli('enable', '--self')
        self.assertIn('认不出', str(caught.exception))
        self.assertFalse((self.home / 'teach.json').exists())

    def test_list_shows_kind_and_expose_tier(self):
        self.add(kind='principle')
        self.add(text='预算在共享盘', mid='om_2', kind='pointer')
        output = self.run_cli('list')
        self.assertIn('t-0001', output)
        self.assertIn('原则', output)
        self.assertIn('可以直接说', output)
        self.assertIn('只能自己知道', output)

    def test_revert_appends_disable_record(self):
        entry = self.add()
        self.run_cli('revert', entry['id'])
        current = self.teach.state(self.home)
        self.assertEqual(current['charter'], [])
        self.assertEqual(current['rev'], 2)

    def test_revert_last_withdraws_the_latest_entry_without_asking_for_an_id(self):
        # 本人说「刚才那条不算」时模型跑的是这条：不用先查 id，回执里再报撤了哪条。
        self.teach.append(self.home, self.teach.new_entry(
            'principle', '引用文件时给绝对路径', 'om_1', 1793790000000))
        payload = json.loads(self.run_cli('revert', '--last'))
        self.assertEqual(payload['id'], 't-0001')
        self.assertEqual(payload['label'], '原则')
        self.assertEqual(payload['text'], '引用文件时给绝对路径')
        self.assertEqual(self.teach.state(self.home)['charter'], [])
        self.assertFalse(json.loads(self.run_cli('revert', '--last'))['ok'])

    def test_a_name_can_be_used_wherever_an_id_could(self):
        # 本人说「撤回 汇报先给结论」，模型照名字查，结果和用编号一样。
        added = json.loads(self.run_cli('add', '--kind', 'style', '--text', '以后汇报先给结论',
                                        '--message-id', 'om_9', '--name', '汇报先给结论'))
        self.assertEqual(added['name'], '汇报先给结论')
        self.assertIn('「汇报先给结论」', self.run_cli('list'))
        payload = json.loads(self.run_cli('revert', '汇报先给结论'))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['id'], added['id'])
        self.assertEqual(payload['name'], '汇报先给结论')
        self.assertEqual(self.teach.state(self.home)['charter'], [])

    def test_rename_changes_only_the_name_and_reports_json(self):
        added = json.loads(self.run_cli('add', '--kind', 'principle', '--text', '不要称呼我为刘总',
                                        '--message-id', 'om_8'))
        payload = json.loads(self.run_cli('rename', added['id'], '称呼宝哥'))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['name'], '称呼宝哥')
        self.assertIn('「称呼宝哥」', self.run_cli('list'))
        self.assertIn('不要称呼我为刘总', self.run_cli('list'))
        # 名称也能当目标（本人只记得名字）
        payload = json.loads(self.run_cli('rename', '称呼宝哥', '叫我宝哥'))
        self.assertEqual(payload['name'], '叫我宝哥')

    def test_revert_of_an_unknown_id_reports_why_instead_of_exiting_nonzero(self):
        # 消费者读的是 JSON：失败原因要能写进回复末尾的回执，而不是变成非零退出。
        payload = json.loads(self.run_cli('revert', 't-9999'))
        self.assertFalse(payload['ok'])
        self.assertIn('没有这条教材', payload['reason'])

    def test_reflect_prints_the_report_and_changes_nothing(self):
        # 复盘以前是 bridge 里的精确命令；现在模型自己跑这个只读命令，报告与 turns 同一份。
        from bridge import turns
        for index in range(3):
            turns.record(self.home, message_id='om_%d' % index, event_id='e%d' % index,
                         outcome='failed', error='AgentFailed')
        path = self.home / 'teachings.jsonl'
        before = path.read_bytes() if path.exists() else b''
        output = self.run_cli('reflect')
        self.assertIn('复盘（最近', output)
        self.assertIn('失败 3 次', output)
        after = path.read_bytes() if path.exists() else b''
        self.assertEqual(before, after)
        self.assertNotIn('<at', output)

    def test_add_records_a_model_verdict_and_is_idempotent_by_message(self):
        output = self.run_cli('add', '--kind', 'style', '--text', '称呼本人为「某哥」',
                              '--message-id', 'om_9')
        payload = json.loads(output)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['id'], 't-0001')
        self.assertFalse(payload['duplicate'])
        self.assertEqual([entry['text'] for entry in self.teach.state(self.home)['charter']],
                         ['称呼本人为「某哥」'])
        # 同一句重放（消费者重试）只得到 duplicate，教材里仍然只有一条。
        again = json.loads(self.run_cli('add', '--kind', 'style', '--text', '称呼本人为「某哥」',
                                        '--message-id', 'om_9'))
        self.assertTrue(again['duplicate'])
        self.assertEqual(len(self.teach.state(self.home)['charter']), 1)

    def test_add_rejects_an_empty_body_without_writing(self):
        payload = json.loads(self.run_cli('add', '--kind', 'fact', '--text', '   ',
                                          '--message-id', 'om_9'))
        self.assertFalse(payload['ok'])
        self.assertTrue(payload['reason'])
        self.assertFalse(self.teach.log_path(self.home).exists())

    def test_status_surfaces_bad_lines(self):
        self.add()
        with self.teach.log_path(self.home).open('a') as handle:
            handle.write('{oops\n')
        payload = json.loads(self.run_cli('status'))
        self.assertEqual(payload['rev'], 1)
        self.assertEqual(payload['charter_count'], 1)
        self.assertEqual(payload['bad_lines'], 1)
        self.assertIn('读不出来', payload['error'])
        self.assertEqual(payload['pending_questions'], 0)

    def test_disable_keeps_entries_but_stops_intake(self):
        self.add()
        (self.home / 'teach.json').write_text(json.dumps(
            {'schema': 1, 'enabled': True, 'enabled_at_ms': 1, 'chat_id': 'oc_self'}))
        self.run_cli('disable')
        config = json.loads((self.home / 'teach.json').read_text())
        self.assertFalse(config['enabled'])
        self.assertEqual(config['chat_id'], 'oc_self')
        self.assertEqual(len(self.teach.state(self.home)['charter']), 1)

    def test_disable_when_never_enabled_is_a_friendly_noop(self):
        output = self.run_cli('disable')
        self.assertIn('本来就没启用', output)
        self.assertFalse((self.home / 'teach.json').exists())

    def test_reachable_from_top_level_command(self):
        output = self.run_cli('status')
        self.assertIn('"rev": 0', output)

    def test_unknown_subcommand_is_a_friendly_error(self):
        with self.assertRaises(SystemExit):
            self.run_cli('nonsense')


class TurnsCommandTests(unittest.TestCase):
    """`feishu-bridge turns`：归因日志的只读出口。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)
        from bridge import turns as turn_log
        self.turn_log = turn_log

    def tearDown(self):
        self._directory.cleanup()

    def run_cli(self, *argv):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'turns'] + [str(arg) for arg in argv]), \
                contextlib.redirect_stdout(stream):
            code = cli.main(str(self.home))
        output = stream.getvalue()
        return code, (json.loads(output) if output.strip() else None)

    def test_turns_are_listed_newest_first(self):
        self.turn_log.record(self.home, message_id='om_1', event_id='e1', outcome='sent',
                             used_ids=['t-0001'])
        self.turn_log.record(self.home, message_id='om_2', event_id='e2', outcome='failed',
                             error='AgentFailed')
        code, output = self.run_cli('--limit', 5)
        self.assertEqual(code, 0)
        self.assertEqual(output['count'], 2)
        self.assertEqual([item['message_id'] for item in output['turns']], ['om_2', 'om_1'])
        self.assertEqual(output['turns'][1]['used_ids'], ['t-0001'])

    def test_outcome_filter_hides_the_rest(self):
        self.turn_log.record(self.home, message_id='om_1', event_id='e1', outcome='sent')
        self.turn_log.record(self.home, message_id='om_2', event_id='e2', outcome='failed',
                             error='AgentFailed')
        code, output = self.run_cli('--outcome', 'failed')
        self.assertEqual(code, 0)
        self.assertEqual([item['message_id'] for item in output['turns']], ['om_2'])

    def test_summary_gives_counts_without_listing_every_turn(self):
        self.turn_log.record(self.home, message_id='om_1', event_id='e1', outcome='sent')
        self.turn_log.record(self.home, message_id='om_2', event_id='e2', outcome='timeout',
                             error='External reply timeout')
        code, output = self.run_cli('--summary')
        self.assertEqual(code, 0)
        self.assertEqual(output['by_outcome'],
                         {'sent': 1, 'skipped': 0, 'failed': 0, 'timeout': 1})
        self.assertEqual(output['turns'], 2)  # 统计：只给数，不列每条明细

    def test_an_unknown_outcome_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            self.run_cli('--outcome', 'mostly-fine')


class LatencyCommandTests(unittest.TestCase):
    """`feishu-bridge latency`：三段耗时的唯一读数口（只读）。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)
        store = Store(str(self.home / 'state.sqlite3'))
        self.db = store.connection
        for index in range(3):
            base = 1_000 + index * 100
            self.db.execute('INSERT INTO inbox(id,state,received_at,create_ms,text) VALUES (?,?,?,?,?)',
                            ('m%d' % index, 'completed', base + 10, base * 1000, 'q'))
            self.db.execute('''INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,
                                                    created_at,state,first_send_at,sent_at,verified_at)
                               VALUES (?,?,1,'reply',?,?,?,?,?,?,?,?)''',
                            ('m%d' % index, 'oc_lat', 'u%d' % index, '答', 'h', base + 30,
                             'reply_verified', base + 34, base + 34, base + 35))
        self.db.commit()
        store.close()

    def tearDown(self):
        self._directory.cleanup()

    def run_cli(self, *argv):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'latency'] + [str(arg) for arg in argv]), \
                contextlib.redirect_stdout(stream):
            code = cli.main(str(self.home))
        output = stream.getvalue()
        return code, (json.loads(output) if output.strip() else None)

    def test_the_command_reports_all_three_stages(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(output['stages']['collect']['p50'], 10.0)
        self.assertEqual(output['stages']['consumer']['p50'], 20.0)
        self.assertEqual(output['stages']['deliver']['p50'], 5.0)
        self.assertEqual(output['samples']['answered'], 3)

    def test_the_window_argument_limits_the_samples(self):
        code, output = self.run_cli('--window', '1')
        self.assertEqual(code, 0)
        self.assertEqual(output['window'], 1)
        self.assertEqual(output['samples']['answered'], 1)

    def test_a_missing_home_reports_zero_instead_of_a_traceback(self):
        code, output = self.run_cli()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'latency']), contextlib.redirect_stdout(stream):
            code = cli.main(other.name)
        self.assertEqual(code, 0)
