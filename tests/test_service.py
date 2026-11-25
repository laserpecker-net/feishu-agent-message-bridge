from tests.identity_doubles import UserModeDouble
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bridge.store import reply_uuid
from bridge.service import (MAX_SCAN_FAILURES, WINDOW_DEFAULT_MS, WINDOW_FLOOR_MS, WINDOW_GROWTH_SUCCESSES,
                            Service, running_release, scan_bounds, self_chat_ids)
from bridge.transport import TransportError

def _row(store, mid):
    """测试助手：按飞书消息 id 取 outbox 行号（v8 里行号与消息 id 是两个标识符）。"""
    return store.get_outbox_by_inbox(mid)['row_id']


def wait_for(predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeTransport(UserModeDouble):
    def __init__(self, message, changed=False, wrong_reply=False):
        self.message=message;self.sent=[];self.changed=changed;self.wrong_reply=wrong_reply;self.before_send=None
        self.texts=[];self.text_uuids=[];self.reply_uuids=[];self.deleted=[]
        self.next_reply_id='reply';self.reply_error=None
    def context(self, message):return []
    def probe_group(self, chat):pass
    def inventory(self):return [{'chat_id':'c','name':'test'}]
    def get_message(self, mid):
        if mid=='reply':
            return {'message_id':'reply','chat_id':'c','parent_id':'wrong' if self.wrong_reply else 'om_input','sender':{'id':'ou_target'},'body':{'content':json.dumps({'text':self.sent[-1]})}}
        m=dict(self.message)
        if self.changed and self.sent==[]:
            # Details read sees original; the next pre-send read sees an edit.
            if getattr(self,'already_read',False):m=dict(m,body={'content':'{"text":"edited"}'})
            self.already_read=True
        if getattr(self,'on_read',None):self.on_read()
        return m
    def reply(self, original, body, uuid, before_send=None):
        if self.before_send:self.before_send()
        if before_send:before_send()
        if self.reply_error is not None:raise self.reply_error
        self.sent.append(body);self.reply_uuids.append(uuid);return self.next_reply_id
    def send_text(self, chat_id, body, dedupe_uuid=None):
        self.texts.append((chat_id, body));self.text_uuids.append(dedupe_uuid);return 'om_say'
    def delete_message(self, message_id):
        self.deleted.append(message_id);return True


class SearchTransport:
    """Offline transport double for the collector: no subprocess, no network."""
    SCOPES = {'im:chat:read', 'im:message', 'im:message.group_msg:get_as_user',
              'im:message.send_as_user', 'search:message', 'im:message.p2p_msg:get_as_user'}
    def __init__(self, result=None, error=None, scopes=None):
        self.result = result or {'ids': [], 'next_token': None, 'notice': None}
        self.error = error
        self.calls = []
        self.scopes = self.SCOPES if scopes is None else set(scopes)
    def identity(self):return {'userName':'tester','openId':'ou_target','available':True}
    def user_scopes(self):return set(self.scopes)
    def self_chat_id(self):return 'oc_auto_self'
    def inventory(self):return [{'chat_id':'c','name':'test'}]
    def search(self, start_ms, end_ms, page_token=None, chat_id=None):
        self.calls.append((start_ms, end_ms, page_token, chat_id))
        if self.error is not None:
            raise self.error
        return dict(self.result)


def build_service(test, config=None, transport=None):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    home = Path(folder.name)
    base = {'target_open_id': 'ou_target', 'sending_enabled': False, 'agent_timeout': 60,
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


def queue_reply(service, mid='om_input', body='reply body', consumer='dsh-test'):
    """把一条消息推进到"回复已写好、等 sender 发出"（走外部消费者发布路径，唯一的发布入口）。"""
    now = time.time()
    service.store.begin_scan('live', 0, 1)
    service.store.ingest_search_page('live', [mid], None, now)
    details = service.store.claim_message('details', now, 100)
    service.store.complete_details(mid, details['attempt_id'], now, {
        'version': 'v1', 'chat_id': 'c', 'sender_id': 's', 'sender_type': 'user',
        'raw_json': '{}', 'text': 'question'}, state='external_queued',
        event={'kind': 'message', 'dedupe_key': 'message:' + mid,
               'payload': {'message_id': mid, 'chat_id': 'c', 'text': 'question'}})
    service.store.register_consumer(consumer, 'dsh', now)
    for event in service.store.claim_events(consumer, now):
        if json.loads(event['payload']).get('message_id') != mid:
            continue
        result = service.store.complete_external_reply(
            consumer, event['id'], now, body, 'uuid-' + mid, claim_token=event['claim_token'])
        assert result.get('ok'), result
        return now
    raise AssertionError('message event was not claimable')


class PipelineTests(unittest.TestCase):
    def test_reduced_search_window_progresses_without_losing_overlap(self):
        start,end=scan_bounds(1000,1000000,9000000,2000)
        self.assertEqual((start,end),(999000,1001000))
        start2,end2=scan_bounds(1000,end,9000000,2000)
        self.assertGreater(end2,end)
        self.assertLess(start2,end)
        self.assertEqual(scan_bounds(1000,2000,8000,4000,origin=2000),(2000,6000))

    def test_scan_bounds_survives_a_backward_clock_jump(self):
        # P2-11: watermark ahead of "now" must never make begin_scan receive end < start.
        start, end = scan_bounds(1000, 9000000, 8000000, 2000)
        self.assertGreater(end, start)
        start, end = scan_bounds(0, 10 ** 13, 10 ** 12, 1000)
        self.assertGreater(end, start)
        start, end = scan_bounds(1000, 1000, 1000, WINDOW_DEFAULT_MS)
        self.assertGreater(end, start)
        self.assertLessEqual(start, 1000)
        start, end = scan_bounds(5000, 5000, 5000, 600000)
        self.assertGreater(end, start)

    def test_running_release_parses_release_directory(self):
        self.assertEqual(running_release('/opt/app/releases/abc123/bridge/service.py'), 'abc123')
        self.assertIsNone(running_release('/opt/app/src/bridge/service.py'))
        self.assertIsNone(running_release())

    def test_default_release_is_fixed_at_import_time(self):
        # current 符号链接会在 install 后指向新 release；进程仍跑旧代码时不能改口。
        from bridge import service as service_module
        with patch.object(service_module, '__file__', '/opt/app/releases/newid/bridge/service.py'):
            self.assertEqual(service_module.running_release(), service_module._LOADED_RELEASE)

    def run_pipeline(self, changed=False, wrong_reply=False, pause_during_send=False, expire_during_send=False):
        """details → 事件 → 消费者回写 → sender。唯一的发布入口是消费者，测试里就扮这个消费者。"""
        folder=tempfile.TemporaryDirectory();home=Path(folder.name)
        config={'target_open_id':'ou_target','sending_enabled':True,'lark_binary':'/fake'}
        (home/'config.json').write_text(json.dumps(config))
        service=Service(home,config)
        message={'message_id':'om_input','chat_id':'c','create_time':str(int(time.time()*1000)),
                 'sender':{'id':'cli_other','sender_type':'app'},'mentions':[{'id':'ou_target'}],
                 'msg_type':'text','body':{'content':'{"text":"37×19?"}'}}
        service.transport=FakeTransport(message,changed,wrong_reply)
        if pause_during_send:
            def pause():
                config['sending_enabled']=False;(home/'config.json').write_text(json.dumps(config))
            service.transport.before_send=pause
        if expire_during_send:
            service.transport.before_send=lambda:service.store.recover(time.time()+4000)
        service.store.set_meta('service_epoch_ms',1000);service.store.register_groups([{'chat_id':'c','name':'test'}],time.time())
        service.store.begin_scan('live',1000,int(time.time()*1000));service.store.ingest_search_page('live',['om_input'],None,time.time())
        service.authenticated.set()
        service.store.register_consumer('dsh-test','dsh',time.time())

        def consume():
            while not service.stop.is_set():
                events = service.store.claim_events('dsh-test', time.time())
                for event in events:
                    service.store.complete_external_reply(
                        'dsh-test', event['id'], time.time(), '37×19=703。',
                        reply_uuid('om_input'), claim_token=event['claim_token'])
                service.stop.wait(0.05)

        threads=[threading.Thread(target=getattr(service,name),daemon=True) for name in ['details','sender']]
        threads.append(threading.Thread(target=consume,daemon=True))
        for thread in threads:thread.start()
        deadline=time.time()+12
        while time.time()<deadline:
            out=service.store.get_outbox_by_inbox('om_input')
            terminal=['reply_verified','needs_attention']
            if pause_during_send:terminal.append('retry_wait')
            if expire_during_send:terminal.append('retry_wait')
            if out and out['state'] in terminal:break
            time.sleep(.05)
        service.stop.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        return folder,service

    def test_receive_analyze_send_and_readback_exactly_once(self):
        folder,s=self.run_pipeline()
        try:
            self.assertEqual(s.store.get_outbox_by_inbox('om_input')['state'],'reply_verified')
            self.assertEqual(s.store.get_message('om_input')['state'],'completed')
            self.assertEqual(len(s.transport.sent),1)
            # 正文由消费者提交什么就发什么：桥不再自己渲染/追加任何东西。
            self.assertEqual(s.transport.sent[0], '37×19=703。')
        finally:s.store.close();folder.cleanup()

    def test_source_edit_prevents_stale_send(self):
        """来源被改写/撤回：回复不发，落明确终态 ``dropped``，**不产生待办**。

        待办的定义是「人还能做点什么」；消息都改了，没有可做的事，挂着只会让 doctor 永远 warn。
        """
        folder,s=self.run_pipeline(changed=True)
        try:
            self.assertEqual(s.store.get_outbox_by_inbox('om_input')['state'],'dropped')
            self.assertEqual(s.store.get_message('om_input')['state'],'completed')
            self.assertEqual(s.transport.sent,[])
            self.assertEqual(s.store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE kind='attention'").fetchone()[0], 0)
        finally:s.store.close();folder.cleanup()

    def test_bad_readback_never_resends(self):
        folder,s=self.run_pipeline(wrong_reply=True)
        try:
            self.assertEqual(s.store.get_outbox_by_inbox('om_input')['state'],'needs_attention')
            self.assertEqual(len(s.transport.sent),1)
        finally:s.store.close();folder.cleanup()

    def test_pause_during_dispatch_wait_blocks_external_send(self):
        folder,s=self.run_pipeline(pause_during_send=True)
        try:
            self.assertEqual(s.transport.sent,[])
            self.assertEqual(s.store.get_outbox_by_inbox('om_input')['state'],'retry_wait')
        finally:s.store.close();folder.cleanup()

    def test_lease_expiry_during_dispatch_wait_blocks_external_send(self):
        # P1-01: nothing was dispatched, so the attempt returns to retry_wait instead of being
        # promoted to the terminal outcome_unknown state.
        folder,s=self.run_pipeline(expire_during_send=True)
        try:
            self.assertEqual(s.transport.sent,[])
            self.assertEqual(s.store.get_outbox_by_inbox('om_input')['state'],'retry_wait')
            self.assertIsNone(s.store.get_outbox_by_inbox('om_input')['first_send_at'])
        finally:s.store.close();folder.cleanup()

    def test_send_guard_rechecks_pause_and_expired_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);config={'target_open_id':'ou_target','sending_enabled':True,'lark_binary':'/fake'}
            (home/'config.json').write_text(json.dumps(config));s=Service(home,config);s.authenticated.set()
            try:
                queue_reply(s)
                outgoing=s.store.claim_outbox(time.time(),120)
                self.assertIsNone(s.store.get_outbox_by_inbox('om_input')['first_send_at'])
                guard=s.send_guard(outgoing)
                guard()
                # P1-01: the guard stamps the dispatch marker before the real request.
                self.assertIsNotNone(s.store.get_outbox_by_inbox('om_input')['first_send_at'])
                config['sending_enabled']=False;(home/'config.json').write_text(json.dumps(config))
                with self.assertRaises(Exception):guard()
                config['sending_enabled']=True;(home/'config.json').write_text(json.dumps(config))
                s.store.recover(time.time()+4000)
                self.assertEqual(s.store.get_outbox_by_inbox('om_input')['state'],'outcome_unknown')
                with self.assertRaises(Exception):guard()
            finally:s.store.close()
    def test_app_thread_budget_survives_rolling_window_expiry(self):
        store = build_service(self).store
        store.app_recent = Mock(return_value=0)
        store.conversation_replies = Mock(return_value=2)
        job = {'id': 'new', 'chat_id': 'c', 'sender_id': 'app', 'sender_type': 'app',
               'root_id': 'old-root'}
        self.assertTrue(store.app_reply_blocked(job, 99999))
        store.conversation_replies.assert_called_once_with('c', 'app', 'old-root')
        self.assertFalse(store.app_reply_blocked(dict(job, sender_type='user'), 99999))
    def test_app_flat_conversations_are_capped_per_day(self):
        # P2-13: flat top-level messages always form a fresh conversation key, so the daily
        # per-app budget is the only lifetime backstop.
        store = build_service(self).store
        store.conversation_replies = Mock(return_value=0)
        store.app_recent = Mock(side_effect=lambda chat, sender, since: 20 if since <= 20000 else 0)
        now = 100000
        job = {'id': 'flat', 'chat_id': 'c', 'sender_id': 'app', 'sender_type': 'app'}
        self.assertTrue(store.app_reply_blocked(job, now, daily_cap=20))
        calls = [call[0] for call in store.app_recent.call_args_list]
        self.assertIn(('c', 'app', now - 600), calls)
        self.assertIn(('c', 'app', now - 86400), calls)
        store.app_recent.side_effect = lambda chat, sender, since: 0
        self.assertFalse(store.app_reply_blocked(job, now, daily_cap=20))
class ReminderTests(unittest.TestCase):
    """到点提醒：服务心跳里抢一条发一条。"""

    def test_a_due_reminder_is_sent_once_without_at_anyone(self):
        transport = Mock()
        transport.send_text.return_value = 'om_reminder'
        service = build_service(self, transport=transport)
        now_ms = int(time.time() * 1000)
        row, duplicate = service.store.add_reminder(now_ms - 1000, '给张三打电话', '打电话给张三',
                                                    'oc_owner', 'om_source', now_ms - 3600000)
        self.assertFalse(duplicate)
        service.fire_reminders()
        body = transport.send_text.call_args[0][1]
        self.assertIn('⏰ 提醒你：给张三打电话', body)
        self.assertNotIn('@', body)  # @ 人会让 bridge 收到自己的消息，等于自己派活
        # 提醒是 agent 主动发的，必须带署名：不然本人以为这是他自己发的。
        self.assertIn('此回复信息来自', body)
        self.assertEqual(transport.send_text.call_args[0][0], 'oc_owner')
        self.assertEqual(service.store.reminders(), [])  # 发过就不在待发列表里
        service.fire_reminders()  # 再跑一拍不会重复发
        self.assertEqual(transport.send_text.call_count, 1)

    def test_a_reminder_that_is_not_due_yet_is_left_alone(self):
        transport = Mock()
        service = build_service(self, transport=transport)
        now_ms = int(time.time() * 1000)
        service.store.add_reminder(now_ms + 600000, '给张三打电话', '打电话给张三', 'oc_owner',
                                   'om_source', now_ms)
        service.fire_reminders()
        self.assertFalse(transport.send_text.called)
        self.assertEqual(len(service.store.reminders()), 1)

    def test_a_failed_send_is_retried_then_given_up_on(self):
        transport = Mock()
        transport.send_text.side_effect = TransportError('boom', 'network')
        service = build_service(self, transport=transport)
        now_ms = int(time.time() * 1000)
        service.store.add_reminder(now_ms - 1000, '给张三打电话', '打电话给张三', 'oc_owner',
                                   'om_source', now_ms)
        for _ in range(3):
            service.fire_reminders()
        self.assertEqual(transport.send_text.call_count, 3)
        # 三次都失败就不再无限重试；失败原因留在库里，`remind status` 能看见。
        service.fire_reminders()
        self.assertEqual(transport.send_text.call_count, 3)
        failed = [row for row in service.store.reminders(live_only=False) if row['state'] == 'failed']
        self.assertEqual(len(failed), 1)
        self.assertIn('boom', failed[0]['last_error'])

    def test_a_reminder_stuck_in_sending_is_recovered_on_startup(self):
        # 抢到之后、标记 sent 之前进程被杀：不能静默丢一条提醒（due 只查 pending）。
        service = build_service(self)
        now_ms = int(time.time() * 1000)
        row, _ = service.store.add_reminder(now_ms - 1000, '给张三打电话', '打电话给张三',
                                            'oc_owner', 'om_source', now_ms)
        self.assertIsNotNone(service.store.claim_reminder(row['id']))
        self.assertEqual(service.store.due_reminders(now_ms), [])
        self.assertEqual(service.store.recover_reminders(), 1)
        self.assertEqual([item['id'] for item in service.store.due_reminders(now_ms)], [row['id']])

    def test_registering_the_same_source_message_twice_keeps_one_reminder(self):
        service = build_service(self)
        now_ms = int(time.time() * 1000)
        service.store.add_reminder(now_ms + 60000, '给张三打电话', '打电话给张三', 'oc_owner',
                                   'om_source', now_ms)
        row, duplicate = service.store.add_reminder(now_ms + 999999, '别的', '别的', 'oc_owner',
                                                    'om_source', now_ms)
        self.assertTrue(duplicate)
        self.assertEqual(row['text'], '给张三打电话')

    def test_a_held_bridge_does_not_fire_and_then_says_it_is_late(self):
        # 挂起期间不响；恢复后这一批带"晚了多少分钟"补发，而不是假装按时。
        transport = Mock()
        transport.send_text.return_value = 'om_reminder'
        service = build_service(self, transport=transport)
        now_ms = int(time.time() * 1000)
        service.store.add_reminder(now_ms - 15 * 60000, '给张三打电话', '打电话给张三',
                                   'oc_owner', 'om_source', now_ms - 3600000)
        service.fire_reminders()
        body = transport.send_text.call_args[0][1]
        self.assertIn('晚了 15 分钟', body)


class CollectorWindowTests(unittest.TestCase):
    def test_notice_ingests_partial_ids_and_shrinks_configured_width_to_floor(self):
        # P1-02: shrink by half of the *configured* width, never below the floor, and keep the
        # IDs the platform did return before dropping the cursor.
        transport = SearchTransport(result={'ids': ['om_a', 'om_b'], 'next_token': None,
                                            'notice': {'incomplete': True}})
        service = build_service(self, transport=transport)
        service.store.set_meta('live_window_successes', 3)
        widths = []
        for _ in range(9):
            outcome = service.collect_scan('live')
            self.assertIsNotNone(outcome['notice'])
            widths.append(service.store.meta('live_window_ms'))
        self.assertEqual(widths[0], WINDOW_DEFAULT_MS // 2)
        self.assertTrue(all(width >= WINDOW_FLOOR_MS for width in widths))
        self.assertEqual(service.store.meta('live_window_ms'), WINDOW_FLOOR_MS)
        self.assertEqual(service.store.meta('live_window_successes'), 0)
        self.assertEqual(service.store.get_message('om_a')['state'], 'details_pending')
        self.assertEqual(service.store.get_message('om_b')['state'], 'details_pending')
        self.assertEqual(service.store.get_scan('live')['active'], 0)
        self.assertIsNone(service.store.get_scan('live')['page_token'])

    def test_window_grows_after_configured_successes_and_caps_at_default(self):
        transport = SearchTransport(result={'ids': [], 'next_token': None, 'notice': None})
        service = build_service(self, transport=transport)
        service.store.set_meta('live_window_ms', WINDOW_FLOOR_MS)
        for _ in range(WINDOW_GROWTH_SUCCESSES - 1):
            service.collect_scan('live')
        self.assertEqual(service.store.meta('live_window_ms'), WINDOW_FLOOR_MS)
        self.assertEqual(service.store.meta('live_window_successes'), WINDOW_GROWTH_SUCCESSES - 1)
        service.collect_scan('live')
        self.assertEqual(service.store.meta('live_window_ms'), WINDOW_FLOOR_MS * 2)
        self.assertEqual(service.store.meta('live_window_successes'), 0)
        service.store.set_meta('live_window_ms', WINDOW_DEFAULT_MS // 2)
        service.store.set_meta('live_window_successes', WINDOW_GROWTH_SUCCESSES - 1)
        service.collect_scan('live')
        self.assertEqual(service.store.meta('live_window_ms'), WINDOW_DEFAULT_MS)
        service.store.set_meta('live_window_successes', WINDOW_GROWTH_SUCCESSES - 1)
        service.collect_scan('live')
        self.assertEqual(service.store.meta('live_window_ms'), WINDOW_DEFAULT_MS)

    def test_collector_survives_watermark_ahead_of_wall_clock(self):
        # P2-11: a backward clock jump leaves the watermark in the future; the collector must
        # clamp the bounds instead of raising ValueError out of begin_scan on every tick.
        transport = SearchTransport(result={'ids': [], 'next_token': None, 'notice': None})
        service = build_service(self, transport=transport)
        future = int(time.time() * 1000) + 3600000
        service.store.begin_scan('live', future - 1000, future)
        service.store.ingest_search_page('live', [], None, time.time())
        self.assertGreater(service.store.get_scan('live')['watermark_ms'], int(time.time() * 1000))
        outcome = service.collect_scan('live')
        self.assertIsNone(outcome['notice'])
        start, end, _token, _chat = transport.calls[-1]
        self.assertGreater(end, start)

    def test_persistent_scan_failure_resets_cursor_and_keeps_backfill_running(self):
        # P1-03: a retryable error repeated MAX_SCAN_FAILURES times must reset the poisoned
        # cursor, and the failure backoff must still hand time slices to backfill.
        error = TransportError('Lark search exploded', 'api', retryable=True)
        transport = SearchTransport(error=error)
        service = build_service(self, transport=transport, config={
            'live_interval_seconds': 0.01, 'backfill_interval_seconds': 0.01,
            'collector_backoff_seconds': 0.005, 'collector_max_backoff_seconds': 0.05,
            'collector_poll_seconds': 0.01})
        resets = []
        original_reset = service.store.reset_scan
        def record_reset(name):
            resets.append(name)
            return original_reset(name)
        service.store.reset_scan = record_reset
        started = []
        original_begin = service.store.begin_scan
        def record_begin(name, start_ms, end_ms):
            started.append((name, end_ms - start_ms))
            return original_begin(name, start_ms, end_ms)
        service.store.begin_scan = record_begin
        thread = threading.Thread(target=service.collector, daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_for(lambda: 'live' in resets, timeout=10), 'live scan was never reset')
            self.assertGreaterEqual(len(transport.calls), MAX_SCAN_FAILURES)
            self.assertTrue(wait_for(lambda: any(name == 'backfill' and span > 3600000 for name, span in started),
                                     timeout=10), 'backfill never got a time slice')
        finally:
            service.stop.set()
            thread.join(5)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertFalse(thread.is_alive())
        health = service.health['collector']
        for key in ('window_ms', 'watermark_lag_s', 'consecutive_failures', 'last_success_at'):
            self.assertIn(key, health)
        # Generic failures must not shrink the search window (only a notice does that).
        self.assertIsNone(service.store.meta('live_window_ms'))

    def test_non_retryable_failure_resets_cursor_immediately(self):
        error = TransportError('Validation failed', 'validation', retryable=False)
        service = build_service(self, transport=SearchTransport(error=error), config={
            'live_interval_seconds': 0.01, 'backfill_interval_seconds': 0.01,
            'collector_backoff_seconds': 0.005, 'collector_max_backoff_seconds': 0.05,
            'collector_poll_seconds': 0.01})
        resets = []
        original_reset = service.store.reset_scan
        service.store.reset_scan = lambda name: (resets.append(name), original_reset(name))[1]
        thread = threading.Thread(target=service.collector, daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_for(lambda: 'live' in resets, timeout=10), 'non-retryable error never reset')
        finally:
            service.stop.set()
            thread.join(5)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertLess(len(service.transport.calls), MAX_SCAN_FAILURES + 4)

    def test_collector_runs_backfill_immediately_and_publishes_health(self):
        # P2-14: the 24h backfill sweep starts with the service instead of one hour later.
        transport = SearchTransport(result={'ids': [], 'next_token': None, 'notice': None})
        service = build_service(self, transport=transport, config={
            'live_interval_seconds': 0.01, 'backfill_interval_seconds': 0.01,
            'collector_poll_seconds': 0.01})
        started = []
        original_begin = service.store.begin_scan
        def record_begin(name, start_ms, end_ms):
            started.append((name, end_ms - start_ms))
            return original_begin(name, start_ms, end_ms)
        service.store.begin_scan = record_begin
        thread = threading.Thread(target=service.collector, daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_for(lambda: any(name == 'backfill' and span > 3600000 for name, span in started),
                                     timeout=10), 'backfill never ran at startup')
            self.assertTrue(wait_for(lambda: 'collector' in service.health, timeout=10))
            # live 扫描可能排在 backfill 之后（间隔只有 0.01s，但机器忙时会明显滞后），
            # 不能假设"跑完上面两条它一定已经开始"：等到它出现再断言水位，语义不变。
            self.assertTrue(wait_for(lambda: service.store.get_scan('live') is not None, timeout=10),
                            'live scan never started')
        finally:
            service.stop.set()
            thread.join(5)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        health = service.health['collector']
        self.assertEqual(health['window_ms'], WINDOW_DEFAULT_MS)
        self.assertIsInstance(health['watermark_lag_s'], float)
        self.assertEqual(health['consecutive_failures'], 0)
        self.assertIsNotNone(health['last_success_at'])
        self.assertIsNone(service.health['errors'].get('collector'))
        self.assertGreater(service.store.get_scan('live')['watermark_ms'], 0)


class SelfScanTests(unittest.TestCase):
    """教材私聊走正常采集：一个专门的 `self` 扫描，只搜那个会话里的 @ 自己。"""

    def service(self, enabled=True, chat='oc_self', enabled_at_ms=0):
        transport = SearchTransport()
        service = build_service(self, transport=transport)
        if enabled:
            (service.home / 'teach.json').write_text(json.dumps(
                {'schema': 1, 'enabled': True, 'enabled_at_ms': enabled_at_ms,
                 'chat_id': chat, 'max_entries': 200, 'max_item_chars': 500,
                 'max_inject_chars': 4000}))
        return service, transport

    def test_the_first_self_page_registers_its_own_bounds(self):
        # 注册只有一个地方：collect_scan 的首次分支（start < end 是飞书的硬要求）。
        service, transport = self.service()
        self.assertIsNone(service.store.get_scan('self'))
        service.collect_scan('self')
        scan = service.store.get_scan('self')
        self.assertIsNotNone(scan)
        self.assertLess(scan['start_ms'], scan['end_ms'])

    def test_a_sub_second_window_is_never_used_for_a_search(self):
        # 真机 2026-11-06（同事 Mac）：启用教学后第一次扫描的区间只有 246ms，
        # 截到秒变成同一个时刻 → 飞书拒 → 每一拍重试同一对边界，消息一条也进不来。
        from bridge.service import MIN_SCAN_SPAN_MS, _usable_bounds
        self.assertFalse(_usable_bounds({'start_ms': 1000, 'end_ms': 1000 + MIN_SCAN_SPAN_MS - 1}))
        self.assertTrue(_usable_bounds({'start_ms': 1000, 'end_ms': 1000 + MIN_SCAN_SPAN_MS}))
        service, transport = self.service()
        service.store.begin_scan('self', 10 ** 12, 10 ** 12 + 246)
        service.collect_scan('self')
        start, end, _token, chat_id = transport.calls[0]
        self.assertEqual(chat_id, 'oc_self')
        self.assertGreaterEqual(end - start, MIN_SCAN_SPAN_MS)

    def test_the_first_tick_after_enabling_waits_until_the_window_is_usable(self):
        now_ms = int(time.time() * 1000)
        service, transport = self.service(enabled_at_ms=now_ms - 200)
        outcome = service.collect_scan('self')
        self.assertTrue(outcome.get('skipped'))
        self.assertIsNone(service.store.get_scan('self'))
        self.assertEqual(transport.calls, [])

    def test_after_five_minutes_without_a_self_chat_the_reason_says_so(self):
        # 认不出会话有两种：刚启动（别动）和这个账号还没有自己的会话（要发一条消息）。
        from bridge import teach
        service, _transport = self.service(enabled=False)
        service.store.set_meta('self_channel_first_try_ms',
                               int((time.time() - teach.SELF_CHAT_DISCOVERY_WAIT_S - 5) * 1000))
        self.assertEqual(service.teach_channel_status()['reason'], 'no_self_chat')
        service.store.set_meta('self_channel_first_try_ms', int(time.time() * 1000))
        self.assertEqual(service.teach_channel_status()['reason'], 'detecting')

    def test_the_private_chat_is_enabled_by_default(self):
        # 没有 teach.json 也算开通：会话自己认出来并登记，用户不需要"登记"这一步。
        service, transport = self.service(enabled=False)
        self.assertIsNone(service.store.get_scan('self'))
        service.ensure_self_channel()
        config = json.loads((service.home / 'teach.json').read_text())
        self.assertTrue(config['enabled'])
        self.assertEqual(config['chat_id'], 'oc_auto_self')
        self.assertTrue(config['auto'])
        # 刚登记出来的区间不足 1 秒（飞书 search 只到秒），那一拍应跳过；给个可用区间再搜。
        now_ms = int(__import__('time').time() * 1000)
        service.store.begin_scan('self', now_ms - 5000, now_ms)
        service.collect_scan('self')
        self.assertEqual(transport.calls[0][3], 'oc_auto_self')

    def test_an_explicit_disable_is_not_undone_by_the_default(self):
        service, _transport = self.service(enabled=False)
        (service.home / 'teach.json').write_text(json.dumps({'schema': 1, 'enabled': False}))
        service.ensure_self_channel()
        self.assertFalse(json.loads((service.home / 'teach.json').read_text())['enabled'])

    def test_without_the_p2p_read_scope_the_default_channel_stays_off(self):
        transport = SearchTransport(scopes=SearchTransport.SCOPES - {'im:message.p2p_msg:get_as_user'})
        service = build_service(self, transport=transport)
        service.ensure_self_channel()
        self.assertFalse((service.home / 'teach.json').exists())
        self.assertEqual(service.teach_channel_status()['reason'], 'no_p2p_scope')

    def test_disabling_teaching_removes_the_self_scan(self):
        service, _transport = self.service()
        service.collect_scan('self')
        (service.home / 'teach.json').write_text(json.dumps({'schema': 1, 'enabled': False}))
        service.sync_self_scan()
        self.assertFalse(service.store.get_scan('self')['active'])

    def test_self_scan_searches_only_the_teach_chat(self):
        service, transport = self.service()
        service.sync_self_scan()
        service.collect_scan('self')
        self.assertEqual(len(transport.calls), 1)
        start, end, _token, chat_id = transport.calls[0]
        self.assertEqual(chat_id, 'oc_self')
        # 真机踩过：注册时 start==end，飞书直接拒（start_time must be earlier than end_time）。
        self.assertLess(start, end)

    def test_group_scans_stay_group_scoped(self):
        service, transport = self.service()
        service.store.begin_scan('live', 0, 1)
        service.store.reset_scan('live')
        service.collect_scan('live')
        self.assertIsNone(transport.calls[0][3])

    def test_status_reports_the_teach_channel(self):
        service, _transport = self.service()
        service.collect_scan('self')
        report = service.teach_channel_status()
        self.assertTrue(report['enabled'])
        self.assertIsInstance(report['watermark_lag_s'], float)
        self.assertLess(report['watermark_lag_s'], 60)

    def test_status_says_so_when_teaching_is_off(self):
        # 没配置时默认开通，所以"没开"必须带原因，不能只说一句 enabled:false。
        service, _transport = self.service(enabled=False)
        status = service.teach_channel_status()
        self.assertFalse(status['enabled'])
        self.assertTrue(status['default_on'])
        self.assertIn(status['reason'], ('detecting', 'no_p2p_scope'))

    def test_a_poisoned_cursor_is_recomputed_instead_of_failing_forever(self):
        # 旧版本注册出 start==end（飞书直接拒）；新版本必须当场重算，而不是每拍重试同一对边界。
        service, transport = self.service()
        service.store.begin_scan('self', 1000, 1000)
        service.store.get_scan('self')
        service.collect_scan('self')
        start, end, _token, _chat = transport.calls[-1]
        self.assertLess(start, end)

    def test_self_scan_never_backfills_before_teaching_was_enabled(self):
        enabled = int(time.time() * 1000) - 60000
        service, transport = self.service(enabled_at_ms=enabled)
        service.sync_self_scan()
        service.collect_scan('self')
        start_ms = transport.calls[0][0]
        self.assertGreaterEqual(start_ms, enabled)


class DispatchGuardTests(unittest.TestCase):
    def test_undispatched_attempts_never_become_outcome_unknown(self):
        # P1-01: 60 failed pre-dispatch attempts (nothing ever left the process) must stay
        # retryable, even across a crash-recovery pass 27 hours later.
        service = build_service(self, config={'sending_enabled': True})
        now = queue_reply(service)
        for _ in range(60):
            outgoing = service.store.claim_outbox(now, 120)
            self.assertIsNotNone(outgoing)
            self.assertEqual(outgoing['mode'], 'send')
            self.assertIsNone(service.store.get_outbox_by_inbox('om_input')['first_send_at'])
            self.assertTrue(service.store.fail_outbox(_row(service.store, 'om_input'), outgoing['attempt_id'], now,
                                                      'pre-dispatch abort', 0))
        self.assertEqual(service.store.get_outbox_by_inbox('om_input')['state'], 'retry_wait')
        service.store.recover(now + 100000)
        self.assertEqual(service.store.get_outbox_by_inbox('om_input')['state'], 'retry_wait')
        self.assertNotEqual(service.store.get_message('om_input')['state'], 'needs_attention')

    def test_dispatched_attempt_expires_into_outcome_unknown(self):
        # P1-01: after the guard stamps dispatch, a 3300s-expired attempt is genuinely
        # uncertain and must surface as outcome_unknown + inbox needs_attention.
        service = build_service(self, config={'sending_enabled': True})
        now = queue_reply(service)
        outgoing = service.store.claim_outbox(now, 120)
        service.send_guard(outgoing)()
        self.assertIsNotNone(service.store.get_outbox_by_inbox('om_input')['first_send_at'])
        service.store.recover(now + 3301)
        self.assertEqual(service.store.get_outbox_by_inbox('om_input')['state'], 'outcome_unknown')
        self.assertEqual(service.store.get_message('om_input')['state'], 'needs_attention')

    def test_send_guard_rejects_an_attempt_whose_lease_is_too_short(self):
        service = build_service(self, config={'sending_enabled': True})
        now = queue_reply(service)
        outgoing = service.store.claim_outbox(now, 30)
        with self.assertRaises(TransportError):
            service.send_guard(outgoing)()


class RetryBoundTests(unittest.TestCase):
    def test_details_lease_and_bounded_retries(self):
        # P2-05: details retries stop at 8 attempts like analysis, with a 150s lease.
        service = build_service(self)
        job = {'id': 'om_d', 'message_id': 'om_d', 'attempt_id': 'a1', 'attempts': 8,
               'chat_id': 'c', 'sender_id': 's', 'sender_type': 'user', 'raw_json': '{}'}
        captured = {}
        service.store.claim_message = lambda stage, now, lease: (captured.setdefault('lease', lease), job)[1]
        def retry(mid, attempt, now, error, delay, permanent):
            captured.update(mid=mid, delay=delay, permanent=permanent)
            return True
        service.store.retry_message = retry
        def explode(mid): raise TransportError('Deterministic retryable failure', 'api', retryable=True)
        service.transport.get_message = explode
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_for(lambda: 'permanent' in captured, timeout=10))
        finally:
            service.stop.set()
            thread.join(5)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(captured['lease'], 150)
        self.assertIs(captured['permanent'], True)
        self.assertEqual(captured['delay'], 60)

class LifecycleTests(unittest.TestCase):
    def test_enabled_fails_safe_on_missing_torn_or_mismatched_config(self):
        # P2-10: a broken config must disable sending, not kill the worker thread.
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            service = Service(home, {'cursor_binary': '/fake', 'lark_binary': '/fake'})
            try:
                self.assertFalse(service.enabled())  # missing config.json + missing target key
                (home / 'config.json').write_text('{not json')
                self.assertFalse(service.enabled())
                (home / 'config.json').write_text(json.dumps({'sending_enabled': True,
                                                              'target_open_id': 'someone-else'}))
                self.assertFalse(service.enabled())
                (home / 'config.json').write_text(json.dumps({'sending_enabled': True,
                                                              'target_open_id': 'ou_target'}))
                service.config = {'target_open_id': 'ou_target'}
                self.assertTrue(service.enabled())
            finally:
                service.store.close()

    def test_run_survives_a_failed_status_cycle(self):
        # P2-10: one failed status write must not take the whole service down.
        service = build_service(self, config={'sending_enabled': True})
        calls = []
        for name in ('collector', 'details', 'sender'):
            setattr(service, name, lambda: service.stop.wait(10))
        service.config['heartbeat_seconds'] = 0.01
        def failing_status():
            calls.append(time.time())
            service.stop.set()
            raise RuntimeError('disk full while writing status.json')
        service.write_status = failing_status
        service.run()  # must not raise
        self.assertEqual(len(calls), 1)
        self.assertTrue(service.store._closed)

    def test_status_snapshot_reports_running_release(self):
        service = build_service(self)
        captured = {}
        with patch('bridge.service.atomic_json', lambda path, value: captured.update(value)):
            service.write_status()
        self.assertIn('running_release', captured)
        self.assertEqual(captured['running_release'], running_release())
        self.assertEqual(captured['workers'], {})
        self.assertIn('stats', captured)
        self.assertFalse(captured['sending_enabled'])


class SelfChatTests(unittest.TestCase):
    """Self-chat is opt-in: only owner messages in allowlisted oc_ chats skip own_message."""
    def service(self, config=None, sender=None, mentions=None):
        service = build_service(self, config=config)
        message = {'message_id': 'om_input', 'chat_id': 'oc_self',
                   'create_time': str(int(time.time() * 1000)),
                   'sender': sender or {'id': 'ou_target', 'sender_type': 'user'},
                   'mentions': [{'id': 'ou_target'}] if mentions is None else mentions,
                   'msg_type': 'text', 'body': {'content': '{"text":"@_user_1 1+1?"}'}}
        service.transport = FakeTransport(message)
        service.store.begin_scan('live', 0, 1)
        service.store.ingest_search_page('live', ['om_input'], None, time.time())
        return service

    def run_details(self, service):
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        wait_for(lambda: service.store.get_message('om_input')['state'] != 'details_pending')
        service.stop.set()
        thread.join(3)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        return service.store.get_message('om_input')

    def test_own_message_stays_ignored_without_allowlist(self):
        message = self.run_details(self.service())
        self.assertEqual((message['state'], message['error']), ('ignored', 'own_message'))

    def test_self_addressed_group_message_is_still_recorded_as_a_group(self):
        """群里 @ 自己派活（self_chat_ids 白名单）仍然是一条群消息。

        2026-11-16 真机：工作群在 self_chat_ids 里，于是 v7 把它的消息标成 chat_type=p2p，
        群场景的 prompt 整段失效（回复继续「宝哥…」）。群/私聊的判据只能是群清单。
        """
        service = self.service(config={'self_chat_ids': ['oc_self']})
        service.store.register_groups([{'chat_id': 'oc_self', 'name': 'work'}], time.time())
        self.run_details(service)
        service.store.register_consumer('dsh-main', 'dsh', time.time())
        payload = json.loads(service.store.claim_events('dsh-main', time.time())[0]['payload'])
        self.assertEqual(payload['chat_type'], 'group')

    def test_allowlisted_self_chat_processes_own_message_without_group_membership(self):
        message = self.run_details(self.service(config={'self_chat_ids': ['oc_self']}))
        self.assertEqual(message['state'], 'external_queued')

    def test_allowlist_does_not_bypass_the_mention_requirement(self):
        message = self.run_details(self.service(config={'self_chat_ids': ['oc_self']}, mentions=[]))
        self.assertEqual((message['state'], message['error']), ('ignored', 'not_personal_mention'))

    def test_allowlist_only_covers_the_named_chat(self):
        service = self.service(config={'self_chat_ids': ['oc_other']})
        message = self.run_details(service)
        self.assertEqual((message['state'], message['error']), ('ignored', 'own_message'))

    def test_other_bot_messages_are_processed_by_default(self):
        # 默认保持旧契约：sender_type=app 的消息照旧进分析队列（有人靠应用消息做集成）。
        service = self.service(config={'self_chat_ids': ['oc_self']},
                               sender={'id': 'cli_bot', 'sender_type': 'app'})
        message = self.run_details(service)
        self.assertEqual(message['state'], 'external_queued')

    def test_other_bot_messages_can_be_disabled(self):
        # reply_to_bots=false：别的机器人 @ 本人 → 直接 ignored，不进队列、不消耗配额。
        service = self.service(config={'self_chat_ids': ['oc_self'], 'reply_to_bots': False},
                               sender={'id': 'cli_bot', 'sender_type': 'app'})
        message = self.run_details(service)
        self.assertEqual((message['state'], message['error']), ('ignored', 'bot_message'))

    def test_human_messages_are_still_processed_when_bots_are_disabled(self):
        service = self.service(config={'self_chat_ids': ['oc_self'], 'reply_to_bots': False})
        message = self.run_details(service)
        self.assertEqual(message['state'], 'external_queued')

    def test_invalid_entries_are_ignored(self):
        self.assertEqual(self_chat_ids({'self_chat_ids': 'oc_one'}), {'oc_one'})
        self.assertEqual(self_chat_ids({'self_chat_ids': ['oc_one', 'bad', 3, None]}), {'oc_one'})
        self.assertEqual(self_chat_ids({}), set())
        self.assertEqual(self_chat_ids(None), set())


class ExternalModeTests(unittest.TestCase):
    message = {'message_id': 'om_input', 'chat_id': 'c', 'create_time': str(int(time.time() * 1000)),
               'sender': {'id': 'ou_other', 'sender_type': 'user'},
               'mentions': [{'id': 'ou_target', 'key': '@_user_1', 'name': '某同事'}],
               'msg_type': 'text',
               'body': {'content': '{"text":"37x19?"}'}}

    def service(self):
        service = build_service(self)
        config = json.loads((service.home / 'config.json').read_text())
        config['sending_enabled'] = True
        (service.home / 'config.json').write_text(json.dumps(config))
        service.transport = FakeTransport(dict(self.message))
        (service.home / 'external.json').write_text(json.dumps({'reply_timeout_seconds': 60}))
        service.store.register_groups([{'chat_id': 'c', 'name': 'test'}], time.time())
        service.store.begin_scan('live', 0, 1)
        service.store.ingest_search_page('live', ['om_input'], None, time.time())
        return service

    def run_details(self, service):
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        deadline = time.time() + 10
        while time.time() < deadline:
            if service.store.get_message('om_input')['state'] in ('external_queued', 'needs_attention'):
                break
            time.sleep(0.02)
        service.stop.set()
        thread.join(3)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name

    def test_details_publishes_a_message_event_for_the_consumer(self):
        service = self.service()
        self.run_details(service)
        self.assertEqual(service.store.get_message('om_input')['state'], 'external_queued')
        stats = service.store.event_stats(time.time())
        self.assertEqual(stats['states'], {'pending': 1})
        self.assertEqual(stats['kinds'], {'message': 1})
        service.store.register_consumer('dsh-main', 'dsh', time.time())
        event = service.store.claim_events('dsh-main', time.time())[0]
        payload = json.loads(event['payload'])
        self.assertEqual(payload['message_id'], 'om_input')
        self.assertEqual(payload['text'], '37x19?')
        self.assertEqual(payload['reply']['uuid'], reply_uuid('om_input'))
        self.assertIsInstance(payload['context'], list)

    def test_event_payload_says_group_and_translates_mentions(self):
        # 模型要知道"这条在群里"（决定口气与能不能不回），也要知道 @_user_1 是谁——
        # 真机上它把占位符原样抄进了回复正文。
        service = self.service()
        self.run_details(service)
        service.store.register_consumer('dsh-main', 'dsh', time.time())
        event = service.store.claim_events('dsh-main', time.time())[0]
        payload = json.loads(event['payload'])
        self.assertEqual(payload['chat_type'], 'group')
        self.assertEqual(payload['mentions'], [{'key': '@_user_1', 'name': '某同事'}])

    def test_external_reply_reaches_sender_only_once(self):
        service = self.service()
        self.run_details(service)
        service.store.register_consumer('dsh-main', 'dsh', time.time())
        event = service.store.claim_events('dsh-main', time.time())[0]
        reply = service.store.complete_external_reply('dsh-main', event['id'], time.time(),
                                                      '37x19=703。', reply_uuid('om_input'),
                                                      claim_token=event['claim_token'])
        self.assertTrue(reply['ok'])
        service.store.emit_event('message', 'message:om_input-retry', {'message_id': 'om_input'}, time.time())
        retry = service.store.claim_events('dsh-main', time.time())[0]
        again = service.store.complete_external_reply('dsh-main', retry['id'], time.time(),
                                                      '37x19=703。', reply_uuid('om_input'),
                                                      claim_token=retry['claim_token'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(service.store.get_outbox_by_inbox('om_input')['body'], '37x19=703。')
        service.stop.clear()
        thread = threading.Thread(target=service.sender, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if service.store.get_outbox_by_inbox('om_input')['state'] == 'reply_verified':
                break
            time.sleep(0.02)
        service.stop.set()
        thread.join(3)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(service.store.get_outbox_by_inbox('om_input')['state'], 'reply_verified')
        self.assertEqual(service.store.get_message('om_input')['state'], 'completed')

    def test_a_reply_to_the_teach_chat_is_delivered_not_marked_dead(self):
        # 真机 16:08：私聊里的回答生成好了，sender 却按「群已不在清单」判死（needs_attention）。
        # 教材私聊是 p2p，永远不会出现在 inventory 的群清单里——必须单独放行。
        service = self.service()
        message = dict(self.message, chat_id='oc_teach')
        service.transport = FakeTransport(message)

        def get_message(mid):
            # 回读校验要求回复消息的 chat_id 与原消息一致（真机里本来就是同一个 p2p 会话）。
            if mid == 'reply':
                return {'message_id': 'reply', 'chat_id': 'oc_teach', 'parent_id': 'om_input',
                        'sender': {'id': 'ou_target'},
                        'body': {'content': json.dumps({'text': service.transport.sent[-1]})}}
            return dict(message)
        service.transport.get_message = get_message
        (service.home / 'teach.json').write_text(json.dumps(
            {'schema': 1, 'enabled': True, 'enabled_at_ms': 0, 'chat_id': 'oc_teach'}))
        service.store.begin_scan('live', 0, 1)
        service.store.ingest_search_page('live', ['om_input'], None, time.time())
        self.run_details(service)
        service.store.register_consumer('dsh-main', 'dsh', time.time())
        event = service.store.claim_events('dsh-main', time.time())[0]
        service.store.complete_external_reply('dsh-main', event['id'], time.time(),
                                              '能读，不能改。', reply_uuid('om_input'),
                                              claim_token=event['claim_token'])
        service.stop.clear()
        thread = threading.Thread(target=service.sender, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if service.store.get_outbox_by_inbox('om_input')['state'] in ('reply_verified', 'needs_attention'):
                break
            time.sleep(0.02)
        service.stop.set()
        thread.join(3)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertEqual(service.store.get_outbox_by_inbox('om_input')['state'], 'reply_verified')

if __name__=='__main__':unittest.main()


def teach_message(text='原则：引用文件时给绝对路径', mid='om_t1', chat='oc_self', sender='ou_target',
                  msg_type='text', at_self=True, create_ms=None, parent_id=None):
    message = {'message_id': mid, 'chat_id': chat,
               'create_time': str(create_ms if create_ms is not None else int(time.time() * 1000)),
               'sender': {'id': sender, 'sender_type': 'user'},
               'body': {'content': json.dumps({'text': text})}, 'msg_type': msg_type}
    if at_self:
        message['mentions'] = [{'key': '@_user_1', 'id': {'open_id': 'ou_target'}, 'name': 'me'}]
    if parent_id:
        message['parent_id'] = parent_id
    return message


class TeachEntryTransport(UserModeDouble):
    """教材私聊的 details 入口替身：消息按 ID 取详情，回执记在 replies 里。"""

    def __init__(self, messages=()):
        self.messages = {message['message_id']: message for message in messages}
        self.replies = []
        self.sent = []

    def identity(self):
        return {'userName': 'tester', 'openId': 'ou_target', 'available': True}

    def inventory(self):
        return [{'chat_id': 'oc_self', 'name': 'teach'}]

    def probe_group(self, chat):
        return None

    def context(self, message):
        return []

    def search(self, start_ms, end_ms, page_token=None, chat_id=None):
        return {'ids': [], 'next_token': None, 'notice': None}

    def get_message(self, mid):
        message = self.messages.get(mid)
        if message is None:
            raise AssertionError('unknown message id %r' % mid)
        return dict(message)

    def reply(self, original, body, uuid, before_send=None):
        self.replies.append((original['message_id'], body))
        return 'om_ack'


class ReplyUuidPurposeTests(unittest.TestCase):
    """同一条消息的多条回复（判读回执 / 答案 / 挂起通知）必须各自有幂等键。

    真机 16:11：私聊里先发过判读回执，随后消费者给出的答案用同一个 uuid 去发，
    飞书当成重复投递直接丢弃并返回旧消息 → outbox 报 "Reply readback mismatch"，答案永远发不出去。
    """

    def test_purpose_changes_the_key(self):
        from bridge.store import reply_uuid
        self.assertNotEqual(reply_uuid('om_1'), reply_uuid('om_1', 'teach'))
        self.assertNotEqual(reply_uuid('om_1'), reply_uuid('om_1', 'control'))

    def test_same_purpose_is_stable(self):
        from bridge.store import reply_uuid
        self.assertEqual(reply_uuid('om_1', 'teach'), reply_uuid('om_1', 'teach'))
        self.assertEqual(len(reply_uuid('om_1')), 40)

    def test_a_teach_receipt_does_not_consume_the_answer_key(self):
        # 回执走 transport.reply(uuid=reply_uuid(mid,'teach'))，答案走 outbox 的 reply_uuid(mid)：
        # 两者不同 → 答案不会被飞书当成重复投递。
        from bridge.store import reply_uuid
        self.assertNotEqual(reply_uuid('om_x', 'teach'), reply_uuid('om_x', 'answer'))


class TeachEntryCase(unittest.TestCase):
    """统一入口测试基座：把一条私聊消息真的送进 details 管道，再断言结果。

    教材通道自 2026-11-05 起不再自己轮询会话——它走正常采集（transport.search → inbox →
    details）。所以这里的测试也必须走同一条路，否则"测过的"和"真跑的"又不是一条路。
    """

    def service(self, messages=(), config=None, enabled_at_ms=0, external=False):
        transport = TeachEntryTransport(messages)
        self.transport = transport
        home_config = {'target_open_id': 'ou_target'}
        home_config.update(config or {})
        service = build_service(self, config=home_config, transport=transport)
        (service.home / 'teach.json').write_text(json.dumps(
            {'schema': 1, 'enabled': True, 'enabled_at_ms': enabled_at_ms,
             'chat_id': 'oc_self', 'max_entries': 200, 'max_item_chars': 500,
             'max_inject_chars': 4000}))
        if external:
            (service.home / 'external.json').write_text(json.dumps(
                {'enabled': True, 'reply_timeout_seconds': 60}))
        from bridge import teach as module
        self.teach = module
        return service

    def deliver(self, service, message):
        """送一条消息进详情管道，等它落成非 details_pending 状态。"""
        mid = message['message_id']
        self.transport.messages[mid] = message
        now = time.time()
        service.store.begin_scan('live', 0, int(now * 1000))
        service.store.ingest_search_page('live', [mid], None, now)
        service.stop.clear()
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        wait_for(lambda: service.store.get_message(mid)['state'] != 'details_pending')
        service.stop.set()
        thread.join(3)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        return service.store.get_message(mid)

    def receipts(self):
        return [body for _mid, body in self.transport.replies]

    def last_receipt(self):
        return self.transport.replies[-1][1] if self.transport.replies else ''


class TeachIntakeTests(TeachEntryCase):
    """私聊里 @ 自己 = 教材入口；准入靠 details（@、本人、会话），不靠内容标记。

    2026-11-06 起（本人两次拍板）**判读全交给模型**：命令词、`问：`/`做：`、`原则：` 都不再由
    bridge 解释，任何文本都原样变成一条 `self_intent=infer` 的事件；落教材/派活/答复由模型决定。
    """

    def events(self, service):
        rows = [event for event in service.store.list_events(limit=20)
                if event['kind'] == 'message']
        return [dict(event, payload=json.loads(event['payload'])) for event in rows]

    def assert_handed_to_model(self, service, text, mid='om_t1'):
        row = self.deliver(service, teach_message(text, mid=mid))
        self.assertEqual((row['state'], row['error']), ('external_queued', None))
        payload = self.events(service)[0]['payload']
        self.assertEqual(payload['self_intent'], 'infer')
        # 正文原样交过去（只去掉 @ 占位符：那是消息结构，不是他说的话）。
        self.assertEqual(payload['text'], self.teach.clean_text(text))
        # bridge 不再自己回执、也不落教材：一切都等模型
        self.assertEqual(self.receipts(), [])
        self.assertFalse((service.home / 'teachings.jsonl').exists())
        return payload

    def test_a_teaching_like_sentence_goes_to_the_model_untouched(self):
        service = self.service(external=True)
        self.assert_handed_to_model(service, '引用文件时给绝对路径')

    def test_a_tagged_sentence_is_also_the_models_call(self):
        # 「原则：」不再是协议：模型读得出"他明说要记"，bridge 不再代劳。
        service = self.service(external=True)
        self.assert_handed_to_model(service, '原则：引用文件时给绝对路径')

    def test_command_words_are_plain_text_now(self):
        # 「教材清单」「复盘」「忘掉最后一条」「记」「答」「做」都要模型理解并自己动手
        # （清单/撤回/复盘用 CLI，那些命令写在 prompt 里）。
        for index, text in enumerate(('教材清单', '复盘', '忘掉最后一条', '记', '答', '做')):
            with self.subTest(text=text):
                service = self.service(external=True)
                self.assert_handed_to_model(service, text, mid='om_cmd%d' % index)

    def test_prefixes_are_no_longer_special(self):
        service = self.service(external=True)
        self.assert_handed_to_model(service, '问：你的信息库在哪？')

    def test_mention_placeholder_is_still_cleaned_out_of_the_text(self):
        # 占位符是消息结构，不是本人说的话——留着它会进提示词和教材。
        service = self.service(external=True)
        payload = self.assert_handed_to_model(service, '原则：@_user_1 引用文件时给绝对路径')
        self.assertNotIn('@_user_1', payload['text'])

    def test_message_from_another_chat_is_not_taken_by_the_teach_channel(self):
        service = self.service()
        row = self.deliver(service, teach_message(chat='oc_other'))
        self.assertEqual(row['state'], 'ignored')
        self.assertNotEqual(row['error'], 'teach_channel')
        self.assertEqual(self.events(service), [])
        self.assertEqual(self.receipts(), [])

    def test_message_without_at_never_reaches_the_router(self):
        service = self.service()
        row = self.deliver(service, teach_message(at_self=False))
        self.assertEqual((row['state'], row['error']), ('ignored', 'not_personal_mention'))
        self.assertEqual(self.receipts(), [])

    def test_image_is_rejected_with_a_visible_receipt(self):
        service = self.service()
        self.deliver(service, teach_message(msg_type='image'))
        self.assertFalse((service.home / 'teachings.jsonl').exists())
        self.assertIn('换成文字', self.last_receipt())

    def test_message_before_enabled_at_ms_is_ignored(self):
        created = int(time.time() * 1000) - 60000
        service = self.service(enabled_at_ms=created + 1000)
        row = self.deliver(service, teach_message(create_ms=created))
        self.assertEqual((row['state'], row['error']), ('ignored', 'teach_channel'))
        self.assertEqual(self.events(service), [])
        self.assertEqual(self.receipts(), [])

    def test_hold_command_still_wins_before_the_teach_channel(self):
        # 「下班」不是业务消息：控制通道在分流之前就吃掉它（details 里的顺序：控制 → 教材分流）。
        from bridge import hold
        service = self.service()
        self.deliver(service, teach_message('下班', mid='om_hold'))
        self.assertTrue(hold.read_state(service.home).get('active'))
        self.assertEqual(self.events(service), [])

    def test_a_quoted_reply_is_still_routed_as_an_answer_to_the_ask(self):
        # 引用我发的求助 = 答复：这是**结构信号**（飞书给 parent_id），不是语义判断，所以留在 bridge。
        # 见 docs/HISTORY.md §24：不分引用/不引用就会重演"私聊被求助锁死"。
        service = self.service(external=True)
        event_id = None
        payload = {'message_id': 'om_orig', 'chat_id': 'oc_group', 'text': '要不要调？',
                   'sender_type': 'user', 'context': []}
        service.store.emit_event('message', 'message:om_orig', payload, time.time())
        events = self.events(service)
        event_id = events[0]['id']
        record = self.teach.register_question(service.home, str(event_id), '要不要调？',
                                              'decision', 'om_dm', 'oc_self')
        self.assertIsNotNone(record)
        row = self.deliver(service, teach_message('按 A 做', mid='om_answer', parent_id='om_dm'))
        self.assertEqual(row['state'], 'ignored')
        self.assertIn('按 A 做', self.last_receipt() or '')
        # 原本那条事件被重放：模型拿到本人的答复去回答原问题
        followups = [event for event in service.store.list_events(limit=20)
                     if event['kind'] == 'message'
                     and json.loads(event['payload']).get('owner_answer') == '按 A 做']
        self.assertEqual(len(followups), 1)

    def test_replayed_message_is_not_acknowledged_twice(self):
        # 切到统一入口时第一次扫描会回放窗口：处理过的消息不能再回一条。
        service = self.service()
        self.teach.append(service.home, self.teach.new_entry(
            'principle', '引用文件时给绝对路径', 'om_t1', 1000))
        self.deliver(service, teach_message(mid='om_t1'))
        self.assertEqual(len(self.teach.state(service.home)['charter']), 1)
        self.assertEqual(self.receipts(), [])


class BridgeMessagesNeverAtAnyoneTests(TeachEntryCase):
    """bridge 自己发的每一类消息都不能 @ 人。

    准入判据是「本人 + 私聊 + @ 自己」。只要 bridge 自己有一次 @ 了人，那条回执就会在下一拍
    被当成教材收回来，形成自激。2026-11-06 起教材回执由模型写，bridge 只剩三类自己发的话：
    挂起/恢复通知、图片退回、答复确认——这三类钉在这里。
    """

    def assert_quiet(self, body):
        for mark in ('<at', '@_user', '@所有人', '@all', '@我'):
            self.assertNotIn(mark, body, 'bridge 自己的消息里出现了 mention：%r' % body[:80])

    def test_hold_notices_are_mention_free(self):
        from bridge import hold
        now_ms = int(time.time() * 1000)
        self.assert_quiet(hold.hold_notice(dict(hold.default_state(), active=True, since_ms=now_ms)))
        self.assert_quiet(hold.release_notice(3, since=now_ms))
        self.assert_quiet(hold.release_notice(None, since=now_ms))

    def test_the_two_bridge_receipts_are_mention_free(self):
        service = self.service(external=True)
        self.deliver(service, teach_message(msg_type='image', mid='om_img'))
        self.assert_quiet(self.last_receipt())
        self.teach.register_question(service.home, '1', '要不要调？', 'decision',
                                     'om_dm', 'oc_self')
        self.deliver(service, teach_message('按 A 做', mid='om_a', parent_id='om_dm'))
        self.assert_quiet(self.last_receipt())


class SenderKindCases(unittest.TestCase):
    """PR-2：sender 按 outbox 行发——reply/say/retract 三条路与失败语义。

    reply 的完整链路（含回读校验）在 PipelineTests 里；这里专测新增的两条：
    say 不能把任务标红、retract 要对账到原行、send_guard 的幂等窗口只约束 reply。
    """

    def build(self):
        message = {'message_id': 'om_input', 'chat_id': 'c',
                   'create_time': str(int(time.time() * 1000)),
                   'sender': {'id': 'cli_other', 'sender_type': 'app'},
                   'mentions': [{'id': 'ou_target'}], 'msg_type': 'text',
                   'body': {'content': '{"text":"37×19?"}'}}
        transport = FakeTransport(message)
        service = build_service(self, config={'sending_enabled': True}, transport=transport)
        service.transport = transport
        service.store.register_groups([{'chat_id': 'c', 'name': 'test'}], time.time())
        now = time.time()
        service.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,version,'
            'raw_json,create_ms) VALUES(?,?,?,0,?,?,?,?,?)',
            ('om_input', 'external_queued', now, 'c', 'app', 'v1',
             json.dumps({'message_id': 'om_input', 'chat_id': 'c', 'thread_id': None}),
             int(now * 1000)))
        service.store.connection.commit()
        return service

    def drive(self, service, row_id, wanted, timeout=10):
        """跑 sender 到这一行进入 ``wanted`` 状态为止。"""
        service.stop.clear()
        thread = threading.Thread(target=service.sender, daemon=True)
        thread.start()
        try:
            return wait_for(
                lambda: service.store.get_outbox(row_id)['state'] in wanted, timeout)
        finally:
            service.stop.set()
            thread.join(3)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name

    # --- say ------------------------------------------------------------
    def test_say_with_source_goes_under_that_message(self):
        service = self.build()
        row_id, seq = service.store.append_outbox('om_input', 'say', '收到，我看看')
        self.assertEqual(seq, 1)
        self.assertTrue(self.drive(service, row_id, ('sent',)))
        row = service.store.get_outbox(row_id)
        self.assertEqual(row['state'], 'sent')
        self.assertEqual(row['reply_id'], 'reply')
        self.assertEqual(service.transport.sent, ['收到，我看看'])
        self.assertEqual(service.transport.texts, [])           # 有来源消息就不发到会话里
        # say 不改变任务状态：inbox 还停在原来的队列状态
        self.assertEqual(service.store.get_message('om_input')['state'], 'external_queued')

    def test_proactive_say_goes_to_the_chat_with_the_outbox_uuid(self):
        service = self.build()
        row_id, _ = service.store.append_outbox(None, 'say', '我到公司了', chat_id='oc_owner')
        self.assertTrue(self.drive(service, row_id, ('sent',)))
        row = service.store.get_outbox(row_id)
        self.assertEqual(row['state'], 'sent')
        self.assertEqual(service.transport.texts, [('oc_owner', '我到公司了')])
        # 幂等键来自 outbox 行：重试不会在群里发出第二条
        self.assertEqual(service.transport.text_uuids, [row['uuid']])

    def test_say_transient_failure_waits_without_touching_the_task(self):
        service = self.build()
        service.transport.reply_error = TransportError('rate limited', 'rate_limit')
        row_id, _ = service.store.append_outbox('om_input', 'say', '收到')
        self.assertTrue(self.drive(service, row_id, ('retry_wait',)))
        self.assertEqual(service.store.get_outbox(row_id)['state'], 'retry_wait')
        self.assertEqual(service.store.get_message('om_input')['state'], 'external_queued')

    def test_say_permanent_failure_parks_without_touching_the_task(self):
        service = self.build()
        service.transport.reply_error = TransportError('Recalled parent', 'validation', retryable=False)
        row_id, _ = service.store.append_outbox('om_input', 'say', '收到')
        self.assertTrue(self.drive(service, row_id, ('needs_attention',)))
        self.assertEqual(service.store.get_outbox(row_id)['state'], 'needs_attention')
        self.assertEqual(service.store.get_message('om_input')['state'], 'external_queued')

    # --- retract --------------------------------------------------------
    def test_retract_deletes_the_original_and_marks_it(self):
        service = self.build()
        say_id, _ = service.store.append_outbox('om_input', 'say', '收到')
        self.assertTrue(self.drive(service, say_id, ('sent',)))
        self.assertEqual(service.store.get_outbox(say_id)['reply_id'], 'reply')
        retract_id, seq = service.store.append_outbox('om_input', 'retract', target_mid='reply')
        self.assertEqual(seq, 2)                                 # 排在那条 say 之后
        self.assertTrue(self.drive(service, retract_id, ('sent',)))
        self.assertEqual(service.transport.deleted, ['reply'])
        self.assertEqual(service.store.get_outbox(retract_id)['state'], 'sent')
        target = service.store.get_outbox(say_id)
        self.assertEqual(target['state'], 'sent')                 # 原行的事实不被抹掉
        self.assertIsNotNone(target['retracted_at'])
        self.assertEqual(service.store.get_message('om_input')['state'], 'external_queued')

    def test_retract_refuses_a_message_this_bridge_did_not_send(self):
        service = self.build()
        row_id, _ = service.store.append_outbox('om_input', 'retract',
                                                target_mid='om_someone_else')
        self.assertTrue(self.drive(service, row_id, ('needs_attention',)))
        self.assertEqual(service.transport.deleted, [])
        self.assertIn('not a message this bridge sent',
                      service.store.get_outbox(row_id)['error'])

    # --- send_guard -----------------------------------------------------
    def test_say_guard_ignores_the_reply_idempotency_window(self):
        service = self.build()
        row_id, _ = service.store.append_outbox('om_input', 'say', '收到')
        outgoing = service.store.claim_outbox(time.time(), 120)
        service.store.mark_send_dispatched(row_id, outgoing['attempt_id'], time.time())
        service.store.connection.execute(
            'UPDATE outbox SET first_send_at=? WHERE row_id=?', (time.time() - 4000, row_id))
        service.store.connection.commit()
        service.send_guard(outgoing)()                           # say 不受 3300s 窗口约束


class TaskCardCase(TeachEntryCase):
    """"造一张待批的卡、再让一条私聊回复真的走一遍管道"——审批相关用例的共同基座。

    只放夹具，不放判据：这样别的测试文件可以只借这两招，不用把这里的用例整套再跑一遍。
    """

    def remember(self, service, mid, chat):
        """让一条消息真的进 inbox：``announce(inbox_id=…)`` 要求原消息在库里。"""
        now = time.time()
        service.store.begin_scan('live', 0, int(now * 1000))
        service.store.ingest_search_page('live', [mid], None, now)
        job = service.store.claim_message('details', now, 100)
        service.store.complete_details(mid, job['attempt_id'], now, {
            'version': 'v1', 'chat_id': chat, 'sender_id': 'ou_other', 'sender_type': 'user',
            'raw_json': '{}', 'text': '帮我汇总周报'}, state='ignored')

    def draft_card(self, service, *, chat='oc_group', origin='om_origin', ttl_ok=True):
        self.remember(service, origin, chat)
        now = time.time() if ttl_ok else time.time() - 3600
        task = service.store.draft_task(chat, '把社区同步群这周的消息汇总成一张表', now,
                                        origin_mid=origin, message_id=origin)
        card_mid = 'om_card_%s' % task['id']
        service.store.attach_task_message(task['id'], card_mid, time.time(),
                                          chat_id=None)
        return task, card_mid


class TaskApprovalFlowTests(TaskCardCase):
    """PR-3 全链路：私聊里的一句话 → 判档 → 改账本 → 回群一句。

    走的是真的 details 管道（不是直接调 store）：这样"接上了"与"接错了线"才分得开——
    PR-2 的 store 层判据全绿时，接线仍然是断的（``task_card`` 只被 prompt 定义、没人接）。
    """

    def test_quoted_reply_approves_and_says_it_in_the_group(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        row = self.deliver(service, teach_message('做，但只做①', mid='om_ans', parent_id=card_mid))
        self.assertEqual(row['state'], 'ignored')
        after = service.store.get_task(task['id'])
        self.assertEqual(after['state'], 'running')
        self.assertEqual(after['approval_kind'], 'quoted')
        self.assertEqual(after['approval_mid'], 'om_ans')
        self.assertEqual(after['hints'][0]['text'], '做，但只做①')
        said = service.store.get_outbox_by_inbox('om_origin', kind='say')
        self.assertIn('批了', said['body'])

    def test_unique_pending_without_quote_approves(self):
        service = self.service(external=True)
        task, _card = self.draft_card(service)
        self.deliver(service, teach_message('做', mid='om_ans'))
        after = service.store.get_task(task['id'])
        self.assertEqual(after['state'], 'running')
        self.assertEqual(after['approval_kind'], 'unique_pending')

    def test_two_pending_without_quote_is_not_an_approval(self):
        service = self.service(external=True)
        first, _ = self.draft_card(service, origin='om_origin_1')
        second, _ = self.draft_card(service, origin='om_origin_2')
        self.deliver(service, teach_message('做', mid='om_ans'))
        self.assertEqual(service.store.get_task(first['id'])['state'], 'open')
        self.assertEqual(service.store.get_task(second['id'])['state'], 'open')
        self.assertIn('引用', self.last_receipt())

    def test_small_talk_is_not_taken_as_approval(self):
        # 唯一一张待批，但这句话里没有态度词：不能被"其余文本都算批准"的默认吃掉。
        service = self.service(external=True)
        task, _card = self.draft_card(service)
        self.deliver(service, teach_message('帮我查下明天的天气', mid='om_ans'))
        self.assertEqual(service.store.get_task(task['id'])['state'], 'open')

    def test_quoted_card_with_no_decision_word_asks_once_and_keeps_it_open(self):
        # 引用只证明"在答这张卡"，不证明"同意做"（2026-11-17 收紧）。
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        self.deliver(service, teach_message('我看看', mid='om_ans', parent_id=card_mid))
        after = service.store.get_task(task['id'])
        self.assertEqual(after['state'], 'open')
        self.assertEqual(after['hints'], [], '含糊答复不落账')
        self.assertIn('要我做就回「做」', self.last_receipt())

    def test_the_same_vague_reply_is_not_asked_twice(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        self.deliver(service, teach_message('我看看', mid='om_ans_1', parent_id=card_mid))
        asked = len(service.transport.replies)
        self.deliver(service, teach_message('再看看', mid='om_ans_2', parent_id=card_mid))
        self.assertEqual(len(service.transport.replies), asked,
                         '十分钟内同一张卡不重复追问')
        self.assertEqual(service.store.get_task(task['id'])['state'], 'open')

    def test_an_app_bot_cannot_approve(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        message = teach_message('做', mid='om_ans', parent_id=card_mid, sender='ou_app')
        message['sender']['sender_type'] = 'app'
        self.deliver(service, message)
        self.assertEqual(service.store.get_task(task['id'])['state'], 'open')

    def test_status_shows_how_many_cards_are_waiting(self):
        # "有几张卡在等人"必须能从 status.json 看出来，否则过期作废了也没人知道。
        service = self.service(external=True)
        service.write_status()
        self.assertEqual(json.loads((service.home / 'status.json').read_text())
                         ['pending_approval']['count'], 0)
        self.draft_card(service)
        service.write_status()
        pending = json.loads((service.home / 'status.json').read_text())['pending_approval']
        self.assertEqual(pending['count'], 1)
        self.assertGreaterEqual(pending['oldest_seconds'], 0)

    def test_expired_card_is_cancelled_and_announced(self):
        service = self.service(external=True)
        task, _card = self.draft_card(service, ttl_ok=False)
        self.assertEqual(service.expire_task_cards(time.time()), 1)
        after = service.store.get_task(task['id'])
        self.assertEqual(after['state'], 'cancelled')
        said = service.store.get_outbox_by_inbox('om_origin', kind='say')
        self.assertIn('作废', said['body'])
