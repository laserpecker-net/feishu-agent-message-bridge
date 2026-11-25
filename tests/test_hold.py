"""挂起开关（hold / release）的验收测试。

对应 docs/bridge-hold-switch-requirements.md §10 的断言：挂起时零采集零入队、
恢复时把关闭窗口从时间轴上切掉、控制通道只认本人精确指令、状态可见且幂等。
"""

from tests.identity_doubles import UserModeDouble
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge import cli
from bridge import commands, hold
from bridge.service import Service, WINDOW_DEFAULT_MS, self_chat_ids
from bridge.store import Store


def build_service(test, config=None, transport=None):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    home = Path(folder.name)
    base = {'target_open_id': 'ou_target', 'sending_enabled': True, 'agent_timeout': 60,
            'cursor_binary': '/fake', 'lark_binary': '/fake'}
    base.update(config or {})
    (home / 'config.json').write_text(json.dumps(base))
    service = Service(home, base)
    test.addCleanup(service.store.close)
    service.authenticated.set()
    service.store.set_meta('service_epoch_ms', int(time.time() * 1000) - WINDOW_DEFAULT_MS)
    if transport is not None:
        service.transport = transport
    return service


class HoldStateTests(unittest.TestCase):
    def test_missing_file_means_running(self):
        with tempfile.TemporaryDirectory() as directory:
            state = hold.read_state(directory)
        self.assertFalse(state['active'])
        self.assertFalse(state['corrupt'])

    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = dict(hold.default_state())
            payload.update({'active': True, 'since_ms': 1234567890000, 'by': 'cli', 'reason': '下班'})
            hold.write_state(directory, payload)
            state = hold.read_state(directory)
            leftover = sorted(path.name for path in Path(directory).iterdir())
        self.assertTrue(state['active'])
        self.assertEqual(state['since_ms'], 1234567890000)
        self.assertEqual(state['reason'], '下班')
        self.assertEqual(leftover, ['hold.json'])

    def test_corrupt_file_fails_open(self):
        # 决策 Q7：坏文件不能让服务永久停机，按"未挂起"处理并标记 corrupt。
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'hold.json').write_text('{not json')
            state = hold.read_state(directory)
        self.assertFalse(state['active'])
        self.assertTrue(state['corrupt'])

    def test_non_object_state_fails_open(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'hold.json').write_text('[1,2,3]')
            self.assertFalse(hold.read_state(directory)['active'])

    def test_control_channel_synonyms_are_not_commands(self):
        """控制通道与私聊命令共用一张表：同义词不算命令，不会被当成开关（2026-11-18）。"""
        for word in ('挂起', '恢复', 'hold', 'off', 'release', 'on'):
            with self.subTest(word=word):
                self.assertIsNone(commands.match(word))
        for word in ('下班', '上班'):
            with self.subTest(word=word):
                self.assertIsNotNone(commands.match(word))

    def test_command_matching_is_exact(self):
        # 开关这一侧不再自己认词：判断只在 bridge/commands.py 一处（2026-11-18）。
        self.assertFalse(hasattr(hold, 'command_for'))
        cases = {
            '下班': 'hold', '上班': 'release',
            ' 下班 ': 'hold', '下班。': 'hold', '@_user_1 下班': 'hold',
            # 同义词一律不算命令：主人要的是逐字命令，不是"猜着像就执行"。
            '挂起': None, 'hold': None, 'OFF': None,
            '恢复': None, 'release': None, 'on': None,
            '我下班了': None, '下班吃饭': None, '下班 明天': None, '': None, '状态': 'status',
        }
        for text, expected in cases.items():
            matched = commands.match(text)
            self.assertEqual(matched[0] if matched else None, expected, text)


class DropPendingTests(unittest.TestCase):
    def make_store(self, test):
        folder = tempfile.TemporaryDirectory()
        test.addCleanup(folder.cleanup)
        store = Store(str(Path(folder.name) / 'state.sqlite3'))
        test.addCleanup(store.close)
        return store

    def test_drop_pending_clears_every_unfinished_queue(self):
        store = self.make_store(self)
        now = time.time()
        store.begin_scan('live', 0, 1)
        store.ingest_search_page('live', ['om_1', 'om_2'], None, now)
        job = store.claim_message('details', now, 100)
        store.complete_details('om_1', job['attempt_id'], now,
                               {'chat_id': 'c', 'version': 'v', 'raw_json': '{}', 'text': 'x'},
                               state='external_queued',
                               event={'kind': 'message', 'dedupe_key': 'message:om_1',
                                      'payload': {'message_id': 'om_1'}})
        store.register_consumer('dsh-test', 'dsh', now)
        event = store.claim_events('dsh-test', now)[0]
        store.complete_external_reply('dsh-test', event['id'], now, 'body', 'uuid-1',
                                      claim_token=event['claim_token'])
        store.emit_event('message', 'message:om_9', {'message_id': 'om_9'}, now)

        dropped = store.drop_pending(now, error='Bridge held')
        stats = store.stats()
        scanned = {row['state'] for row in store.connection.execute('SELECT state FROM inbox')}
        outbox_states = {row['state'] for row in store.connection.execute('SELECT state FROM outbox')}
        event_states = {row['state'] for row in store.connection.execute('SELECT state FROM events')}

        # 口径（2026-11-04 统一）：一条消息算一条。这里 2 条未完成消息（inbox 行）
        # + 1 条待发回执（outbox 行）+ 1 条 message 事件（events 行）→ 仍然只报 2，
        # 不再把三类行数加总成 4（旧实现会把 15 条说成 30 条）。
        self.assertEqual(dropped, 2)
        self.assertEqual(store.meta('hold_dropped_detail'),
                         {'messages': 2, 'replies': 1, 'notices': 0})
        self.assertNotIn('details_pending', scanned)
        self.assertNotIn('external_queued', scanned)
        self.assertEqual(outbox_states, {'dropped'})
        # 已发布那条事件已被消费者 ack（done），未处理的那条变成 dead。
        self.assertEqual(event_states, {'done', 'dead'})
        self.assertEqual(sum(stats['attention'][key] for key in
                             ('inbox_needs_attention', 'outbox_needs_attention', 'outcome_unknown')), 0)

    def test_drop_pending_counts_attention_events_once(self):
        # attention 事件没有 inbox 行：它是"另一类待办"，按一件事算一条。
        store = self.make_store(self)
        now = time.time()
        store.begin_scan('live', 0, 1)
        store.ingest_search_page('live', ['om_1'], None, now)
        store.emit_event('message', 'message:om_1', {'message_id': 'om_1'}, now)
        store.emit_event('attention', 'attention:om_2', {'message_id': 'om_2'}, now)
        store.emit_event('attention', 'attention:om_3', {'message_id': 'om_3'}, now)

        dropped = store.drop_pending(now, error='Bridge held')

        self.assertEqual(dropped, 3)  # 1 条消息 + 2 件待办事项
        self.assertEqual(store.meta('hold_dropped_detail'),
                         {'messages': 1, 'replies': 0, 'notices': 2})

    def test_drop_pending_keeps_terminal_rows(self):
        store = self.make_store(self)
        now = time.time()
        store.begin_scan('live', 0, 1)
        store.ingest_search_page('live', ['om_1'], None, now)
        job = store.claim_message('details', now, 100)
        store.complete_details('om_1', job['attempt_id'], now,
                               {'chat_id': 'c', 'version': 'v', 'raw_json': '{}', 'text': 'x'},
                               state='ignored', error='not_personal_mention')
        store.drop_pending(now)
        state = store.connection.execute('SELECT state FROM inbox WHERE id=?', ('om_1',)).fetchone()['state']
        self.assertEqual(state, 'ignored')

    def test_release_floor_moves_epoch_and_scan_cursors(self):
        store = self.make_store(self)
        store.set_meta('service_epoch_ms', 1000)
        store.begin_scan('backfill', 0, 1000)
        store.apply_release_floor(5000)
        self.assertEqual(store.meta('service_epoch_ms'), 5000)
        self.assertEqual(store.meta('backfill_target_ms'), 5000)
        self.assertEqual(store.meta('backfill_position_ms'), 5000)
        for name in ('live', 'backfill'):
            scan = store.get_scan(name) or {}
            self.assertFalse(scan.get('active'))
            if scan:
                self.assertEqual(scan['watermark_ms'], 5000)


class FakeControlTransport(UserModeDouble):
    """离线双：只实现控制通道用到的三个方法。"""

    def __init__(self, messages=None, complete=True):
        self.messages = list(messages or [])
        self.replies = []
        self.polls = 0
        self.complete = complete

    def identity(self):
        return {'userName': 'tester', 'openId': 'ou_target', 'available': True}

    def inventory(self):
        return [{'chat_id': 'c', 'name': 'test'}]

    def chat_messages(self, chat_id, start_ms, end_ms, page_size=50, max_pages=20):
        self.polls += 1
        return [dict(message) for message in self.messages], self.complete

    def context(self, message):
        return []

    def reply(self, original, body, uuid, before_send=None):
        self.replies.append((original['message_id'], body))
        return 'om_reply'


class SearchRecorder:
    """记录搜索调用，用来证明挂起时一次都没搜。"""

    def __init__(self):
        self.calls = []

    def identity(self):
        return {'userName': 'tester', 'openId': 'ou_target', 'available': True}

    def inventory(self):
        return [{'chat_id': 'c', 'name': 'test'}]

    def search(self, start_ms, end_ms, page_token=None, chat_id=None):
        self.calls.append((start_ms, end_ms))
        return {'ids': [], 'next_token': None, 'notice': None}


def control_message(text, mid='om_cmd', chat='oc_self', sender='ou_target', create_ms=None):
    return {'message_id': mid, 'chat_id': chat, 'create_time': create_ms or int(time.time() * 1000),
            'sender': {'id': sender, 'sender_type': 'user'},
            'body': {'content': json.dumps({'text': text})}, 'msg_type': 'text'}


class ServiceHoldTests(unittest.TestCase):
    def test_held_service_never_searches(self):
        service = build_service(self, config={'self_chat_ids': ['oc_self']})
        transport = SearchRecorder()
        service.transport = transport
        state = dict(hold.default_state())
        state.update({'active': True, 'since_ms': int(time.time() * 1000) - 1000, 'by': 'cli'})
        hold.write_state(service.home, state)
        thread = threading.Thread(target=service.collector, daemon=True)
        thread.start()
        time.sleep(1.2)
        service.stop.set()
        thread.join(5)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(transport.calls, [])
        self.assertTrue(hold.read_state(service.home)['active'])

    def test_entering_hold_drops_backlog_and_records_count(self):
        service = build_service(self)
        now = time.time()
        service.store.begin_scan('live', 0, int(now * 1000))
        service.store.ingest_search_page('live', ['om_pending'], None, now)
        service.enter_hold('cli', now, reason='下班')
        self.assertTrue(hold.read_state(service.home)['active'])
        state = service.store.connection.execute(
            'SELECT state FROM inbox WHERE id=?', ('om_pending',)).fetchone()['state']
        self.assertEqual(state, 'dropped')
        self.assertGreaterEqual(service.store.meta('hold_dropped_total'), 1)
        self.assertGreaterEqual(service.store.meta('hold_entry_applied_ms'), int(now * 1000) - 1000)
        self.assertGreater(service.store.meta('control_cursor_ms'), 0)

    def test_cli_and_feishu_release_receipts_are_byte_identical(self):
        """同一次恢复，终端和飞书里「上班」必须给出同一段文字（同一个函数产出）。"""
        service = build_service(self)
        now = time.time()
        service.enter_hold('cli', now - 600, reason='下班')
        service.store.set_meta('hold_dropped_current', 7)
        feishu_notice = service.release_hold('cli', now)
        self.assertEqual(feishu_notice, hold.release_notice(7, since=hold.read_state(service.home).get('since_ms'))
                         .replace('· 挂起期间无积压消息', '· 挂起期间无积压消息'))
        self.assertIn('丢弃 7 条消息', feishu_notice)
        # 终端侧：等服务应用后读同一个 meta，产出完全一致的文案
        from bridge import cli
        dropped = cli._wait_release_applied(service.home, int(now * 1000), timeout_seconds=0.5)
        self.assertEqual(dropped, 7)
        self.assertEqual(hold.release_notice(dropped, since=hold.read_state(service.home).get('since_ms')),
                         feishu_notice)

    def test_cli_release_reports_unknown_instead_of_claiming_zero(self):
        """服务没跑（或数据库不可用）时不能编"无积压消息"——那样会把丢消息说成没丢。"""
        from bridge import cli
        with tempfile.TemporaryDirectory() as directory:
            dropped = cli._wait_release_applied(Path(directory), int(time.time() * 1000),
                                                timeout_seconds=0.2)
        self.assertIsNone(dropped)
        notice = hold.release_notice(dropped, since=int(time.time() * 1000))
        self.assertIn('待服务启动后', notice)
        self.assertNotIn('无积压消息', notice)

    def test_release_moves_epoch_so_off_window_is_excluded_forever(self):
        service = build_service(self)
        now = time.time()
        service.enter_hold('cli', now - 600, reason='下班')
        release_ms = int(now * 1000)
        notice = service.release_hold('cli', now)
        self.assertIn('已恢复', notice)
        self.assertEqual(service.store.meta('service_epoch_ms'), release_ms)
        self.assertEqual(service.store.meta('backfill_target_ms'), release_ms)
        self.assertEqual(service.store.meta('backfill_position_ms'), release_ms)
        self.assertEqual(service.store.meta('hold_release_applied_ms'), release_ms)
        self.assertFalse(hold.read_state(service.home)['active'])

    def test_hold_state_survives_restart(self):
        service = build_service(self)
        service.enter_hold('cli', time.time())
        restarted = Service(service.home, service.config)
        self.addCleanup(restarted.store.close)
        self.assertTrue(restarted.held())
        # 新进程不得自行恢复：epoch 仍是挂起前的值。
        self.assertLess(restarted.store.meta('service_epoch_ms'), int(time.time() * 1000))

    def test_status_reports_held_mode(self):
        service = build_service(self)
        service.enter_hold('cli', time.time(), reason='下班')
        service.write_status()
        status = json.loads((service.home / 'status.json').read_text())
        self.assertEqual(status['mode'], 'held')
        self.assertTrue(status['held'])
        self.assertEqual(status['held_reason'], '下班')
        self.assertGreaterEqual(status['dropped_during_hold'], 0)
        self.assertEqual(status['control_channel'], 'unconfigured')

    def test_control_channel_command_turns_hold_on_and_off(self):
        transport = FakeControlTransport([control_message('下班')])
        service = build_service(self, config={'self_chat_ids': ['oc_self']}, transport=transport)
        service.control_tick(time.time())
        self.assertTrue(hold.read_state(service.home)['active'])
        self.assertEqual(transport.polls, 1)
        self.assertEqual(len(transport.replies), 1)
        self.assertIn('已下班', transport.replies[0][1])

        transport.messages = [control_message('上班', mid='om_resume', create_ms=int(time.time() * 1000))]
        service.control_tick(time.time() + 31)
        self.assertFalse(hold.read_state(service.home)['active'])
        self.assertIn('已恢复', transport.replies[-1][1])
        self.assertEqual(service.store.meta('backfill_target_ms'),
                         hold.read_state(service.home)['last_release_at_ms'])

    def test_control_channel_ignores_strangers_and_chatter(self):
        transport = FakeControlTransport([
            control_message('下班', mid='om_stranger', sender='ou_other'),
            control_message('下班', mid='om_chatter', chat='oc_other'),
            control_message('我今天下班很晚', mid='om_chatter2'),
        ])
        service = build_service(self, config={'self_chat_ids': ['oc_self']}, transport=transport)
        service.control_tick(time.time())
        self.assertFalse(hold.read_state(service.home)['active'])
        self.assertEqual(transport.replies, [])

    def test_control_channel_ignores_history_before_the_floor(self):
        # 重启后控制窗口不能把历史上的"上班"捞出来误恢复。
        transport = FakeControlTransport([control_message('上班', create_ms=int(time.time() * 1000) - 3600000)])
        service = build_service(self, config={'self_chat_ids': ['oc_self']}, transport=transport)
        now = time.time()
        service.enter_hold('cli', now)
        service.store.set_meta('control_cursor_ms', int(now * 1000))
        service.control_tick(now + 31)
        self.assertTrue(hold.read_state(service.home)['active'])
        self.assertEqual(transport.replies, [])

    def test_control_window_incomplete_advances_cursor_only_to_last_read(self):
        """窗口没读完时游标只能停到已读到的位置：推到窗口末尾 = 静默丢弃后面那些消息。"""
        now = time.time()
        read_to = int(now * 1000) - 5000
        transport = FakeControlTransport([control_message('下班', mid='om_first', create_ms=read_to)],
                                         complete=False)
        service = build_service(self, config={'self_chat_ids': ['oc_self']}, transport=transport)
        service.control_tick(now)
        self.assertTrue(hold.read_state(service.home)['active'])
        self.assertEqual(service.store.meta('control_cursor_ms'), read_to)
        self.assertLess(service.store.meta('control_cursor_ms'), int(now * 1000))

    def test_control_message_is_never_answered_as_business_question(self):
        service = build_service(self, config={'self_chat_ids': ['oc_self']})
        self.assertTrue(service.is_control_message(control_message('下班')))
        self.assertFalse(service.is_control_message(control_message('这个 bug 怎么修')))

    def test_held_gates_details_and_sending(self):
        service = build_service(self, config={'self_chat_ids': ['oc_self']})
        service.enter_hold('cli', time.time())
        self.assertTrue(service.held())
        transport = FakeControlTransport()
        service.transport = transport
        threads = [threading.Thread(target=getattr(service, name), daemon=True)
                   for name in ('details', 'sender')]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        service.stop.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(transport.polls, 0)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_after_release_no_scan_covers_the_off_window(self):
        """§10-4：恢复后首次回溯的 start_ms 必须 >= 恢复时刻，关闭窗口永不被回补。"""
        service = build_service(self)
        transport = SearchRecorder()
        service.transport = transport
        floor = int(time.time() * 1000)
        service.store.apply_release_floor(floor)
        thread = threading.Thread(target=service.collector, daemon=True)
        thread.start()
        time.sleep(1.2)
        service.stop.set()
        thread.join(5)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertTrue(transport.calls, 'collector should have searched after release')
        for start, end in transport.calls:
            self.assertGreaterEqual(start, floor)
            self.assertGreaterEqual(end, start)

    def test_details_worker_is_gated_while_held(self):
        service = build_service(self, config={'self_chat_ids': ['oc_self']})
        service.enter_hold('cli', time.time())
        transport = FakeControlTransport()
        service.transport = transport
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        time.sleep(0.3)
        service.stop.set()
        thread.join(5)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(transport.polls, 0)

    def test_command_message_in_self_chat_is_marked_ignored_not_queued(self):
        service = build_service(self, config={'self_chat_ids': ['oc_self']})
        message = control_message('下班')
        service.transport = FakeControlTransport([message])
        service.transport.get_message = lambda mid: dict(message)
        now = time.time()
        service.store.begin_scan('live', 0, int(now * 1000))
        service.store.ingest_search_page('live', ['om_cmd'], None, now)
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        time.sleep(0.8)
        service.stop.set()
        thread.join(5)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        row = service.store.connection.execute(
            'SELECT state,error FROM inbox WHERE id=?', ('om_cmd',)).fetchone()
        self.assertEqual(row['state'], 'ignored')
        # 命令表（bridge/commands.py）接管了 下班/上班：落成 command:hold，仍是终态、不排队。
        self.assertEqual(row['error'], 'command:hold')
        self.assertTrue(hold.read_state(service.home)['active'])


class HoldCliTests(unittest.TestCase):
    def test_hold_then_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_x',
                                                          'self_chat_ids': ['oc_self']}))
            with patch('sys.argv', ['feishu-bridge', 'hold', '--reason', '下班']), \
                    patch('builtins.print'), patch.object(cli, '_send_hold_notice'):
                cli.main(directory)
                state = hold.read_state(home)
                cli.main(directory)
                again = hold.read_state(home)
            self.assertTrue(state['active'])
            self.assertEqual(state['reason'], '下班')
            self.assertEqual(state['since_ms'], again['since_ms'])
            with patch('sys.argv', ['feishu-bridge', 'release']), \
                    patch('builtins.print'), patch.object(cli, '_send_hold_notice'):
                cli.main(directory)
                released = hold.read_state(home)
                cli.main(directory)
            self.assertFalse(released['active'])
            self.assertIsNotNone(released['last_release_at_ms'])

    def test_hold_without_config_exits_with_hint(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['feishu-bridge', 'hold']), patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                cli.main(directory)
        self.assertIn('install.py', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
