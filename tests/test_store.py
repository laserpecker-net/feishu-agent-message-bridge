import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from bridge.store import Store, reply_uuid

def _row(store, mid):
    """测试助手：按飞书消息 id 取 outbox 行号（v8 里行号与消息 id 是两个标识符）。"""
    return store.get_outbox_by_inbox(mid)['row_id']


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def inbox(self, *ids):
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', ids, None, 10)

    def analysis(self, mid='m1', sender_type='user'):
        """把一条消息推进到"等外部消费者领取"的 external_queued（唯一大脑的入口）。"""
        self.inbox(mid)
        job = self.store.claim_message('details', 10, 10)
        self.assertTrue(self.store.complete_details(mid, job['attempt_id'], 11, {
            'version': 'v1', 'chat_id': 'g1', 'sender_id': 's1',
            'sender_type': sender_type, 'raw_json': '{}', 'text': 'hello'}))
        return self.store.get_message(mid)

    def publish(self, mid='m1', body='reply', uuid_value=None, now=13, consumer='dsh-test',
                payload=None, dedupe=None):
        """走外部消费者的发布路径：事件 → 领取 → 提交正文。返回是否写入了 outbox。

        入口判据（正文/UUID 非空）先于认领，和 store 同一条判据——否则非法输入会留下
        一条已认领但永远提交不了的事件，后面的用例就再也领不到它。
        """
        if not isinstance(body, str) or not body.strip():
            raise ValueError('Reply body must be nonempty')
        if uuid_value is not None and (not isinstance(uuid_value, str) or not uuid_value.strip()):
            raise ValueError('Reply UUID must be nonempty')
        self.store.register_consumer(consumer, 'dsh', now)
        self.store.emit_event('message', 'message:' + mid + (dedupe or ''),
                              payload if payload is not None else {'message_id': mid}, now)
        for event in self.store.claim_events(consumer, now):
            if json.loads(event['payload']).get('message_id') != mid:
                continue
            result = self.store.complete_external_reply(
                consumer, event['id'], now, body, uuid_value or ('uuid-' + mid),
                claim_token=event['claim_token'])
            return result.get('ok', False)
        raise AssertionError('message event was not claimable')

    def outgoing(self, mid='m1'):
        self.analysis(mid)
        self.assertTrue(self.publish(mid))
        return self.store.get_outbox_by_inbox(mid)

    def test_wal_full_and_json_meta_survive_restart(self):
        self.assertEqual(self.store.connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(self.store.connection.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertIsNone(self.store.meta('missing'))
        self.assertEqual(self.store.meta('missing', 4), 4)
        self.store.set_meta('object', {'enabled': True, 'unknown': None})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.meta('object'), {'enabled': True, 'unknown': None})

    def test_scan_cursor_deduplicates_and_commits_watermark_only_at_end(self):
        self.inbox('m1')
        self.store.begin_scan('live', 1500, 3000)
        self.store.ingest_search_page('live', ['m1', 'm2', 'm2'], 'next', 11)
        scan = self.store.get_scan('live')
        self.assertEqual((scan['watermark_ms'], scan['active'], scan['page_token']), (2000, 1, 'next'))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_scan('live')['page_token'], 'next')
        self.store.ingest_search_page('live', ['m3'], None, 12)
        self.assertEqual(self.store.get_scan('live')['watermark_ms'], 3000)
        self.assertEqual(self.store.stats()['inbox'], {'details_pending': 3})
        self.assertEqual(self.store.get_message('m1')['received_at'], 10)

    def test_page_failure_rolls_back_ids_and_cursor_together(self):
        self.store.begin_scan('live', 1000, 2000)
        self.store.connection.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON inbox WHEN NEW.id='bad' BEGIN SELECT RAISE(ABORT, 'bad'); END")
        with self.assertRaises(Exception):
            self.store.ingest_search_page('live', ['good', 'bad'], 'next', 10)
        self.assertIsNone(self.store.get_message('good'))
        self.assertIsNone(self.store.get_scan('live')['page_token'])
        self.assertEqual(self.store.get_scan('live')['active'], 1)

    def test_reset_preserves_committed_watermark(self):
        self.inbox('m1')
        self.store.begin_scan('live', 1500, 3000)
        self.store.ingest_search_page('live', ['m2'], 'next', 11)
        self.store.reset_scan('live')
        scan = self.store.get_scan('live')
        self.assertEqual((scan['active'], scan['watermark_ms'], scan['page_token']), (0, 2000, None))

    def test_unknown_or_inactive_scan_cannot_ingest(self):
        with self.assertRaises(ValueError):
            self.store.ingest_search_page('missing', ['m1'], None, 10)
        self.inbox('m1')
        with self.assertRaises(ValueError):
            self.store.ingest_search_page('live', ['m2'], None, 11)
        self.assertIsNone(self.store.get_message('m2'))

    def test_expired_details_attempt_cannot_complete_or_retry_after_reclaim(self):
        self.inbox('m1')
        old = self.store.claim_message('details', 10, 5)
        self.assertFalse(self.store.complete_details('m1', old['attempt_id'], 15, {'text': 'old'}))
        self.store.recover(15)
        fresh = self.store.claim_message('details', 15, 10)
        self.assertNotEqual(old['attempt_id'], fresh['attempt_id'])
        self.assertEqual(fresh['attempts'], 2)
        self.assertFalse(self.store.complete_details('m1', old['attempt_id'], 16, {'text': 'old'}))
        self.assertFalse(self.store.retry_message('m1', old['attempt_id'], 16, 'old', 0))
        self.assertTrue(self.store.complete_details('m1', fresh['attempt_id'], 16, {'text': 'fresh'}, state='ignored'))
        self.assertEqual(self.store.get_message('m1')['text'], 'fresh')

    def test_retry_due_and_permanent_failure(self):
        self.inbox('m1')
        job = self.store.claim_message('details', 10, 20)
        self.assertTrue(self.store.retry_message('m1', job['attempt_id'], 11, 'temporary', 5))
        self.assertIsNone(self.store.claim_message('details', 15, 20))
        job = self.store.claim_message('details', 16, 20)
        self.assertTrue(self.store.retry_message('m1', job['attempt_id'], 17, 'permanent', 0, True))
        self.assertEqual(self.store.get_message('m1')['state'], 'needs_attention')

    def test_details_reject_unapproved_fields_and_bad_state(self):
        self.inbox('m1')
        job = self.store.claim_message('details', 10, 20)
        with self.assertRaises(ValueError):
            self.store.complete_details('m1', job['attempt_id'], 11, {'attempt_id': 'forged'})
        with self.assertRaises(ValueError):
            self.store.complete_details('m1', job['attempt_id'], 11, {}, state='completed')
        self.assertEqual(self.store.get_message('m1')['state'], 'details_running')


    def test_outbox_content_and_uuid_are_immutable(self):
        box = self.outgoing()
        self.assertEqual(box['body_hash'], hashlib.sha256(b'reply').hexdigest())
        self.assertEqual(box['inbox_id'], 'm1')
        # A replay cannot replace a reply even if application state is corrupted.
        self.assertFalse(self.publish('m1', 'different', 'other-uuid', 15, dedupe='-retry'))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['body'], 'reply')
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['uuid'], 'uuid-m1')
        with self.assertRaises(Exception):
            self.store.connection.execute("UPDATE outbox SET body='mutated' WHERE inbox_id='m1'")

    def test_uuid_is_not_reused_for_another_message(self):
        self.outgoing()
        job = self.analysis('m2')
        self.assertFalse(self.publish('m2', 'reply2', 'uuid-m1', 13))
        self.assertIsNone(self.store.get_outbox_by_inbox('m2'))
        self.assertEqual(self.store.get_message('m2')['state'], 'external_queued')

    def test_blank_body_or_uuid_rejected_without_completion(self):
        job = self.analysis()
        for body, uuid in [(' ', 'uuid'), ('reply', ''), ('reply', ' ')]:
            with self.assertRaises(ValueError):
                self.publish('m1', body, uuid, 13)
        self.assertIsNone(self.store.get_outbox_by_inbox('m1'))

    def test_sent_is_not_completed_until_readback_verified(self):
        self.outgoing()
        send = self.store.claim_outbox(14, 10)
        self.assertEqual(send['mode'], 'send')
        self.assertTrue(self.store.outbox_sent(_row(self.store, 'm1'), send['attempt_id'], 15, 'reply-id'))
        self.assertEqual(self.store.get_message('m1')['state'], 'reply_queued')
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'verify_pending')
        self.assertFalse(self.store.outbox_verified(_row(self.store, 'm1'), send['attempt_id'], 16))
        verify = self.store.claim_outbox(16, 10)
        self.assertEqual((verify['mode'], verify['reply_id']), ('verify', 'reply-id'))
        self.assertTrue(self.store.outbox_verified(_row(self.store, 'm1'), verify['attempt_id'], 17))
        self.assertEqual(self.store.get_message('m1')['state'], 'completed')
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'reply_verified')

    def test_recover_send_retry_preserves_original_payload_and_fences_ack(self):
        self.outgoing()
        old = self.store.claim_outbox(14, 10)
        self.store.close()
        self.store = Store(self.path)
        self.store.recover(24)
        fresh = self.store.claim_outbox(24, 10)
        self.assertEqual((fresh['body'], fresh['uuid'], fresh['first_send_at']), ('reply', 'uuid-m1', None))
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'm1'), fresh['attempt_id'], 24))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['first_send_at'], 24)
        self.assertNotEqual(old['attempt_id'], fresh['attempt_id'])
        self.assertFalse(self.store.outbox_sent(_row(self.store, 'm1'), old['attempt_id'], 25, 'late'))
        self.assertTrue(self.store.outbox_sent(_row(self.store, 'm1'), fresh['attempt_id'], 25, 'current'))

    def test_uncertain_send_cutoff_at_3300_seconds_never_resends(self):
        self.outgoing()
        job = self.store.claim_outbox(14, 10)
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'm1'), job['attempt_id'], 14))
        self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], 15, 'timeout', 0))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'retry_wait')
        self.assertIsNone(self.store.claim_outbox(3314, 10))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'outcome_unknown')
        self.assertIsNone(self.store.claim_outbox(9999, 10))

    def test_recovery_cutoff_and_expired_ack(self):
        self.outgoing()
        job = self.store.claim_outbox(14, 4000)
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'm1'), job['attempt_id'], 14))
        self.assertFalse(self.store.outbox_sent(_row(self.store, 'm1'), job['attempt_id'], 4014, 'late'))
        self.store.recover(4014)
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'outcome_unknown')
        self.assertIsNone(self.store.claim_outbox(4015, 10))

    def test_verification_retry_never_sends_again_even_after_cutoff(self):
        self.outgoing()
        send = self.store.claim_outbox(14, 10)
        self.store.outbox_sent(_row(self.store, 'm1'), send['attempt_id'], 15, 'reply-id')
        verify = self.store.claim_outbox(16, 10)
        self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), verify['attempt_id'], 17, 'readback timeout', 10))
        self.assertIsNone(self.store.claim_outbox(26, 10))
        verify = self.store.claim_outbox(4000, 10)
        self.assertEqual(verify['mode'], 'verify')
        self.store.recover(4010)
        fresh = self.store.claim_outbox(4010, 10)
        self.assertEqual(fresh['mode'], 'verify')
        self.assertFalse(self.store.outbox_verified(_row(self.store, 'm1'), verify['attempt_id'], 4011))
        self.assertTrue(self.store.outbox_verified(_row(self.store, 'm1'), fresh['attempt_id'], 4011))

    def test_permanent_outbox_failure_requires_attention(self):
        self.outgoing()
        job = self.store.claim_outbox(14, 10)
        self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], 15, 'source changed', 0, True))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'needs_attention')
        self.assertIsNone(self.store.claim_outbox(16, 10))

    def test_group_absence_requires_two_complete_inventories(self):
        self.store.register_groups([{'chat_id': 'g1', 'name': 'One'}], 10)
        self.assertEqual(self.store.next_group_probe(10)['chat_id'], 'g1')
        self.store.group_access('g1', True, None, 11)
        self.assertIsNone(self.store.next_group_probe(12))
        self.store.register_groups([], 20)
        group = self.store.groups()[0]
        self.assertEqual((group['present'], group['miss_count'], group['last_seen']), (1, 1, 10))
        self.store.register_groups([], 30)
        self.assertEqual(self.store.groups()[0]['present'], 0)
        self.assertEqual(self.store.stats()['groups']['listed'], 0)
        self.store.register_groups([{'chat_id': 'g1', 'name': 'New'}], 40)
        self.assertEqual((self.store.groups()[0]['present'], self.store.groups()[0]['miss_count']), (1, 0))
        self.assertIsNotNone(self.store.next_group_probe(3611))

    def test_group_probe_errors_and_stats(self):
        self.store.register_groups([{'chat_id': name, 'name': name} for name in ['g1', 'g2', 'g3']], 10)
        self.store.group_access('g1', True, None, 11)
        self.store.group_access('g2', False, 'no permission', 11)
        self.assertEqual(self.store.stats()['groups'], {'listed': 3, 'readable': 1, 'unreadable': 1, 'unchecked': 1})
        self.assertEqual(self.store.next_group_probe(12)['chat_id'], 'g3')

    def test_app_loop_count_includes_queued_and_uncertain(self):
        job = self.analysis(sender_type='app')
        self.assertEqual(self.store.app_recent('g1', 's1', 0), 0)
        self.publish('m1', 'reply', 'uuid', 13)
        self.assertEqual(self.store.app_recent('g1', 's1', 13), 1)
        self.assertEqual(self.store.app_recent('g1', 'other', 0), 0)
        self.assertEqual(self.store.app_recent('g1', 's1', 14), 0)
        self.store.claim_outbox(14, 10)
        self.store.recover(3314)
        self.assertEqual(self.store.app_recent('g1', 's1', 0), 1)

    def test_conversation_reply_budget_survives_hours_and_matches_original_root(self):
        def intention(mid, now, root_id=None, parent_id=None, thread_id=None, sender_id='app-1', chat_id='g1', expected=True):
            self.store.begin_scan('live', 1000, 2000)
            self.store.ingest_search_page('live', [mid], None, now)
            detail = self.store.claim_message('details', now, 10)
            self.assertTrue(self.store.complete_details(mid, detail['attempt_id'], now + 1, {
                'version': 'v1', 'chat_id': chat_id, 'sender_id': sender_id, 'sender_type': 'app',
                'root_id': root_id, 'parent_id': parent_id, 'thread_id': thread_id}))
            self.assertEqual(self.publish(mid, 'reply', 'uuid-' + mid, now + 3), expected)
            if not expected:
                # 预算打满时消息进 needs_attention 等人工看，而不是留在队列里空转。
                self.assertEqual(self.store.get_message(mid)['state'], 'needs_attention')
                self.assertIsNone(self.store.get_outbox_by_inbox(mid))

        intention('original', 10)
        self.assertEqual(self.store.conversation_replies('g1', 'app-1', 'original'), 1)
        sent = self.store.claim_outbox(14, 10)
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'original'), sent['attempt_id'], 14))
        self.assertTrue(self.store.fail_outbox(_row(self.store, 'original'), sent['attempt_id'], 15, 'timeout', 0))
        self.assertIsNone(self.store.claim_outbox(3314, 10))
        self.assertEqual(self.store.get_outbox_by_inbox('original')['state'], 'outcome_unknown')
        intention('later-1', 4000, root_id='original', parent_id='intermediate', thread_id='')
        intention('later-2', 8000, root_id='original', expected=False)
        intention('other-root', 12000, root_id='separate')
        intention('other-sender', 16000, root_id='original', sender_id='app-2')
        intention('other-chat', 20000, root_id='original', chat_id='g2')
        self.assertEqual(self.store.conversation_replies('g1', 'app-1', 'original'), 2)
        self.assertEqual(self.store.conversation_replies('g1', 'app-1', 'separate'), 1)
        self.assertEqual(self.store.app_recent('g1', 'app-1', 19000), 0)
        # Thread identity takes priority; empty root falls back to parent.
        intention('threaded', 24000, root_id='original', thread_id='thread-key')
        intention('parented', 28000, root_id='', parent_id='parent-key')
        self.assertEqual(self.store.conversation_replies('g1', 'app-1', 'original'), 2)
        self.assertEqual(self.store.conversation_replies('g1', 'app-1', 'thread-key'), 1)
        self.assertEqual(self.store.conversation_replies('g1', 'app-1', 'parent-key'), 1)

    def test_threaded_claims_each_message_once(self):
        self.inbox(*['m%d' % i for i in range(30)])
        claimed, failures = [], []
        def worker():
            try:
                while True:
                    job = self.store.claim_message('details', 11, 10)
                    if not job:
                        return
                    claimed.append(job['id'])
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        self.assertFalse(failures)
        self.assertEqual(len(claimed), 30)
        self.assertEqual(len(set(claimed)), 30)

    def test_claim_never_stamps_dispatch_and_predispatch_failures_stay_retryable(self):
        self.outgoing()
        now = 14
        for _ in range(60):
            job = self.store.claim_outbox(now, 10)
            self.assertIsNone(job['first_send_at'])
            self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], now + 1, 'offline', 0))
            now += 60
        self.store.recover(now + 1000000)
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'retry_wait')
        self.assertNotEqual(self.store.get_message('m1')['state'], 'needs_attention')

    def test_dispatch_mark_fences_and_only_then_starts_uncertain_window(self):
        self.outgoing()
        job = self.store.claim_outbox(14, 10)
        self.assertIsNone(job['first_send_at'])
        self.assertFalse(self.store.mark_send_dispatched(_row(self.store, 'm1'), 'stale-attempt', 14))
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'm1'), job['attempt_id'], 20))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['first_send_at'], 20)
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'm1'), job['attempt_id'], 22))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['first_send_at'], 20)
        self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], 23, 'timeout', 0))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'retry_wait')
        self.assertIsNone(self.store.claim_outbox(3321, 10))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'outcome_unknown')
        self.assertEqual(self.store.get_message('m1')['state'], 'needs_attention')

    def test_partial_ingest_keeps_cursor_and_watermark(self):
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', ['m1'], 'tok', 10)
        before = self.store.get_scan('live')
        self.assertEqual(self.store.ingest_partial_ids('live', ['m2', 'm1'], 11), 1)
        after = self.store.get_scan('live')
        self.assertEqual((after['active'], after['page_token'], after['watermark_ms']),
                         (before['active'], before['page_token'], before['watermark_ms']))
        self.assertIsNotNone(self.store.get_message('m2'))
        with self.assertRaises(ValueError):
            self.store.ingest_partial_ids('live', [''], 12)
        self.store.reset_scan('live')
        with self.assertRaises(ValueError):
            self.store.ingest_partial_ids('live', ['m3'], 13)
        self.assertIsNone(self.store.get_message('m3'))

    def test_verification_retry_limit_parks_row_for_attention(self):
        self.outgoing()
        send = self.store.claim_outbox(14, 10)
        self.assertTrue(self.store.outbox_sent(_row(self.store, 'm1'), send['attempt_id'], 15, 'reply-id'))
        now = 16
        for _ in range(8):
            job = self.store.claim_outbox(now, 10)
            self.assertEqual(job['mode'], 'verify')
            self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], now + 1, 'readback blocked', 0))
            now += 5
        self.assertIsNone(self.store.claim_outbox(now, 10))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'needs_attention')
        self.assertEqual(self.store.get_message('m1')['state'], 'needs_attention')

    def test_predispatch_failures_do_not_spend_verification_budget(self):
        self.outgoing()
        now = 14
        for _ in range(7):
            job = self.store.claim_outbox(now, 10)
            self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], now + 1, 'offline', 0))
            now += 2
        send = self.store.claim_outbox(now, 10)
        self.assertEqual(send['attempts'], 8)
        self.assertTrue(self.store.outbox_sent(_row(self.store, 'm1'), send['attempt_id'], now + 1, 'reply-id'))
        now += 2
        for index in range(8):
            verify = self.store.claim_outbox(now, 10)
            self.assertIsNotNone(verify)
            self.assertEqual((verify['mode'], verify['verify_attempts'], verify['attempts']),
                             ('verify', index + 1, index + 9))
            self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), verify['attempt_id'], now + 1, 'readback blocked', 0))
            now += 2
        self.assertIsNone(self.store.claim_outbox(now, 10))
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['state'], 'needs_attention')

    def test_stats_attention_and_pending_exclude_terminal_rows(self):
        self.outgoing()
        job = self.store.claim_outbox(14, 10)
        self.assertTrue(self.store.mark_send_dispatched(_row(self.store, 'm1'), job['attempt_id'], 14))
        self.assertTrue(self.store.fail_outbox(_row(self.store, 'm1'), job['attempt_id'], 15, 'timeout', 0))
        self.assertIsNone(self.store.claim_outbox(3321, 10))
        stats = self.store.stats()
        self.assertIsNone(stats['oldest_pending_at'])
        self.assertEqual(stats['attention']['outcome_unknown'], 1)
        self.assertEqual(stats['attention']['outbox_needs_attention'], 0)
        self.assertEqual(stats['attention']['inbox_needs_attention'], 1)
        self.assertIsNotNone(stats['attention']['oldest_attention_at'])
        self.assertIsNotNone(stats['attention']['oldest_outcome_unknown_at'])

    def test_schema_version_pinned_and_future_version_rejected(self):
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],
                         Store.SCHEMA_VERSION)
        self.store.close()
        import sqlite3
        raw = sqlite3.connect(str(self.path))
        raw.execute('PRAGMA user_version=%d' % (Store.SCHEMA_VERSION + 1))  # 比当前支持的高一版
        raw.close()
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_v7_database_is_refused_with_the_migration_command(self):
        # 下界拒绝：v7 库没有 outbox.row_id，直接跑会在第一条查询上炸成
        # "no such column"，把「没迁移」伪装成「代码有 bug」。
        self.store.close()
        import sqlite3
        raw = sqlite3.connect(str(self.path))
        raw.execute('PRAGMA user_version=7')
        raw.close()
        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        self.assertIn('migrate-outbox-v8', str(caught.exception))


class EventChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def external_message(self, mid='m1', now=10, sender_type='user', thread_id=None):
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', [mid], None, now)
        job = self.store.claim_message('details', now, 10)
        payload = {'message_id': mid, 'chat_id': 'g1', 'sender_id': 's1', 'text': 'hello'}
        self.assertTrue(self.store.complete_details(
            mid, job['attempt_id'], now + 1,
            {'version': 'v1', 'chat_id': 'g1', 'sender_id': 's1', 'sender_type': sender_type,
             'thread_id': thread_id, 'raw_json': '{}', 'text': 'hello'},
            state='external_queued',
            event={'kind': 'message', 'dedupe_key': 'message:' + mid, 'payload': payload}))
        return payload

    # --- 过期消息清理 ---------------------------------------------------------
    def _seed_expired(self, now):
        """造三类过期残留：死信事件、挂着的未完成消息、回复确定不发但状态没收口的消息。"""
        old = now - 30 * 86400
        with self.store._transaction() as db:
            db.execute("""INSERT INTO events(kind,dedupe_key,payload,created_at,state)
                          VALUES('message','message:old-dead','{}',?,'dead')""", (old,))
            db.execute("""INSERT INTO events(kind,dedupe_key,payload,created_at,state)
                          VALUES('message','message:old-done','{}',?,'done')""", (old,))
            db.execute("""INSERT INTO events(kind,dedupe_key,payload,created_at,state)
                          VALUES('message','message:new-dead','{}',?,'dead')""", (now,))
            # 未完成、已过期
            db.execute("""INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id)
                          VALUES('om_stale','external_queued',?,0,'oc_a','ou_a')""", (old,))
            db.execute("""INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id)
                          VALUES('om_stale_out','reply_queued',?,0,'oc_a','ou_a')""", (old,))
            db.execute("""INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,created_at,state)
                          VALUES('om_stale_out','oc_a',1,'reply','u-old','b','h',?,'retry_wait')""",
                       (old,))
            # 回复确定不发（outbox=dead），但 inbox 还停在 reply_queued
            db.execute("""INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id)
                          VALUES('om_unsent','reply_queued',?,0,'oc_b','ou_b')""", (old,))
            db.execute("""INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,created_at,state,error)
                          VALUES('om_unsent','oc_b',1,'reply','u-unsent','b','h',?,'dead','外部群不接管')""",
                       (old,))
            # 新鲜的未完成：不能被清理动到
            db.execute("""INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_id)
                          VALUES('om_fresh','details_pending',?,0,'oc_c','ou_c')""", (now - 60,))
        return old

    def test_prune_expired_dry_run_changes_nothing(self):
        now = 100000.0
        self._seed_expired(now)
        report = self.store.prune_expired(now, older_than_s=7 * 86400, dry_run=True)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['events_dead_deleted'], 1)   # 只有 30 天前那条 dead；新的不算
        self.assertEqual(report['events_done_deleted'], 0)   # 默认不删 done
        self.assertEqual(report['inbox_dropped'], 1)
        self.assertEqual(report['outbox_dropped'], 1)
        self.assertEqual(report['inbox_closed_unsent'], 1)
        self.assertEqual(self.store.event_stats(now)['states'], {'done': 1, 'dead': 2})
        self.assertEqual(self.store.get_message('om_stale')['state'], 'external_queued')

    def test_prune_expired_deletes_dead_events_and_closes_stale_work(self):
        now = 100000.0
        self._seed_expired(now)
        report = self.store.prune_expired(now, older_than_s=7 * 86400)
        self.assertEqual(report['events_dead_deleted'], 1)
        self.assertEqual(report['events_done_deleted'], 0)
        self.assertEqual(report['inbox_dropped'], 1)
        self.assertEqual(report['outbox_dropped'], 1)
        self.assertEqual(report['inbox_closed_unsent'], 1)
        # 死信被物理删除，新的 dead 事件保留
        self.assertEqual(self.store.event_stats(now)['states'], {'done': 1, 'dead': 1})
        # inbox/outbox 行不可删（触发器），只能改状态
        self.assertEqual(self.store.get_message('om_stale')['state'], 'dropped')
        self.assertEqual(self.store.get_outbox_by_inbox('om_stale_out')['state'], 'dropped')
        self.assertEqual(self.store.get_message('om_unsent')['state'], 'ignored')
        # 新鲜消息一动不动
        self.assertEqual(self.store.get_message('om_fresh')['state'], 'details_pending')
        # 收口后「待处理」指针指向唯一一条真正待处理的新消息，而不是被作废/收口的历史记录。
        # （dropped = 作废、ignored = 收口，都不该把指针钉在 30 天前。）
        self.assertEqual(self.store.stats()['oldest_pending_at'], now - 60)

    def test_prune_expired_include_done_is_opt_in(self):
        now = 100000.0
        self._seed_expired(now)
        report = self.store.prune_expired(now, older_than_s=7 * 86400, include_done=True)
        self.assertEqual(report['events_done_deleted'], 1)
        self.assertEqual(self.store.event_stats(now)['states'], {'dead': 1})
        self.assertEqual(self.store.prune_expired(now, older_than_s=0)['events_dead_deleted'], 0)

    def test_register_claim_ack_and_competing_consumers(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.store.register_consumer('codex-main', 'codex', 10)
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, 11)
        self.store.emit_event('attention', 'attention:b', {'message_id': 'm2'}, 12)
        first = self.store.claim_events('dsh-main', 13, limit=1)
        second = self.store.claim_events('codex-main', 13, limit=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]['id'], second[0]['id'])
        self.assertEqual(first[0]['consumer'], 'dsh-main')
        self.assertEqual(second[0]['consumer'], 'codex-main')
        self.assertTrue(self.store.ack_event('dsh-main', first[0]['id'], 14, claim_token=first[0]['claim_token']))
        self.assertFalse(self.store.ack_event('codex-main', first[0]['id'], 14, claim_token=first[0]['claim_token']))
        self.assertEqual(self.store.event_stats(15)['states'], {'done': 1, 'leased': 1})

    def test_claim_requires_registered_consumer_and_dedupes_events(self):
        self.assertRaises(LookupError, self.store.claim_events, 'ghost', 10)
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.assertTrue(self.store.emit_event('message', 'message:m1', {'message_id': 'm1'}, 11))
        self.assertFalse(self.store.emit_event('message', 'message:m1', {'message_id': 'm1'}, 12))
        self.assertEqual(len(self.store.claim_events('dsh-main', 13)), 1)
        self.assertEqual(self.store.claim_events('dsh-main', 13), [])

    def test_lease_expiry_redelivers_then_attempt_cap_dead_letters(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.external_message('m1')
        first = self.store.claim_events('dsh-main', 20, lease_seconds=5)
        self.assertEqual(first[0]['attempts'], 1)
        self.assertEqual(self.store.claim_events('dsh-main', 24), [])
        second = self.store.claim_events('dsh-main', 26, lease_seconds=5)
        self.assertEqual(second[0]['attempts'], 2)
        # next_attempt_cap=2: once the second lease expires the event is dead, not re-claimed.
        self.assertEqual(self.store.claim_events('dsh-main', 32, next_attempt_cap=2), [])
        self.assertEqual(self.store.event_stats(32)['states'], {'dead': 1})
        self.assertEqual(self.store.get_message('m1')['state'], 'needs_attention')

    def test_fail_backoff_and_permanent_dead_letter(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, 11)
        job = self.store.claim_events('dsh-main', 12)[0]
        self.assertEqual(self.store.fail_event('dsh-main', job['id'], 13, 'temporary', retry_delay=60,
                                              claim_token=job['claim_token']), 'pending')
        self.assertEqual(self.store.claim_events('dsh-main', 30), [])
        retried = self.store.claim_events('dsh-main', 80)[0]
        self.assertEqual(retried['attempts'], 2)
        self.assertEqual(self.store.fail_event('dsh-main', retried['id'], 81, 'nope', permanent=True,
                                              claim_token=retried['claim_token']), 'dead')
        self.assertEqual(self.store.event_stats(82)['states'], {'dead': 1})
        self.assertFalse(self.store.fail_event('dsh-main', job['id'], 83, 'lost lease', claim_token=job['claim_token']))

    def test_external_reply_writes_immutable_outbox_once(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.external_message('m1')
        job = self.store.claim_events('dsh-main', 20)[0]
        result = self.store.complete_external_reply('dsh-main', job['id'], 21, 'reply body', reply_uuid('m1'),
                                                    claim_token=job['claim_token'])
        self.assertTrue(result['ok'])
        self.assertFalse(result['duplicate'])
        outbox = self.store.get_outbox_by_inbox('m1')
        self.assertEqual(outbox['state'], 'queued')
        self.assertEqual(outbox['uuid'], reply_uuid('m1'))
        self.assertEqual(self.store.get_message('m1')['state'], 'reply_queued')
        self.assertEqual(self.store.event_stats(22)['states'], {'done': 1})
        # A second event/message cannot overwrite the immutable outbox.
        self.store.emit_event('message', 'message:m1-again', {'message_id': 'm1'}, 23)
        again = self.store.claim_events('dsh-main', 24)[0]
        duplicate = self.store.complete_external_reply('dsh-main', again['id'], 25, 'different', 'other-uuid',
                                                       claim_token=again['claim_token'])
        self.assertFalse(duplicate['ok'])
        self.assertEqual(self.store.get_event(again['id'])['state'], 'leased')
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['body'], 'reply body')

    def test_external_reply_requires_lease_and_message_event(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, 11)
        job = self.store.claim_events('dsh-main', 12)[0]
        result = self.store.complete_external_reply('dsh-main', job['id'], 13, 'x', reply_uuid('m1'),
                                                    claim_token=job['claim_token'])
        self.assertFalse(result['ok'])
        self.assertIn('not leased', result['reason'])
        self.store.emit_event('message', 'message:m2', {'message_id': 'm2'}, 14)
        job = self.store.claim_events('dsh-main', 15)[0]
        # Unknown message cannot get an outbox row.
        self.assertFalse(self.store.complete_external_reply('dsh-main', job['id'], 16, 'x', reply_uuid('m2'),
                                                           claim_token=job['claim_token'])['ok'])

    def test_external_skip_closes_the_event_without_any_outbox(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.external_message('m1')
        job = self.store.claim_events('dsh-main', 20)[0]
        result = self.store.complete_external_skip('dsh-main', job['id'], 21,
                                                   '对方只是回了一句哦哦', claim_token=job['claim_token'])
        self.assertTrue(result['ok'])
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['message_id'], 'm1')
        # 一条消息都没发：outbox 里没有这行，inbox 记 ignored 并留下原因
        self.assertIsNone(self.store.get_outbox_by_inbox('m1'))
        message = self.store.get_message('m1')
        self.assertEqual(message['state'], 'ignored')
        self.assertIn('对方只是回了一句哦哦', message['error'])
        self.assertEqual(self.store.event_stats(22)['states'], {'done': 1})
        # 重放同一个 claim_token 是幂等的（崩溃恢复会重跑这一步）
        again = self.store.complete_external_skip('dsh-main', job['id'], 23, '理由一样',
                                                  claim_token=job['claim_token'])
        self.assertTrue(again['ok'])
        self.assertTrue(again['duplicate'])

    def test_external_skip_refuses_after_a_reply_was_queued(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.external_message('m1')
        job = self.store.claim_events('dsh-main', 20)[0]
        self.store.complete_external_reply('dsh-main', job['id'], 21, 'reply body', reply_uuid('m1'),
                                           claim_token=job['claim_token'])
        result = self.store.complete_external_skip('dsh-main', job['id'], 22, '想改成不回',
                                                   claim_token=job['claim_token'])
        self.assertFalse(result['ok'])
        self.assertIn('已经排了回复', result['reason'])
        self.assertEqual(self.store.get_outbox_by_inbox('m1')['body'], 'reply body')

    def test_external_skip_requires_a_leased_message_event_and_a_reason(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, 11)
        job = self.store.claim_events('dsh-main', 12)[0]
        self.assertFalse(self.store.complete_external_skip(
            'dsh-main', job['id'], 13, 'no', claim_token=job['claim_token'])['ok'])
        with self.assertRaises(ValueError):
            self.store.complete_external_skip('dsh-main', job['id'], 14, '   ',
                                             claim_token=job['claim_token'])

    def test_timeout_expires_external_message_and_emits_attention(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.external_message('m1', now=10)
        self.assertEqual(self.store.expire_external(100, 60), 1)
        self.assertEqual(self.store.get_message('m1')['state'], 'needs_attention')
        self.assertEqual(self.store.event_stats(101)['states'], {'dead': 1})
        self.assertEqual(self.store.emit_attention_events(102), 1)
        self.assertEqual(self.store.emit_attention_events(103), 0)

    def test_attention_sweep_is_idempotent(self):
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', ['m1'], None, 10)
        self.store.connection.execute("UPDATE inbox SET state='needs_attention',error='boom' WHERE id='m1'")
        self.assertEqual(self.store.emit_attention_events(20), 1)
        self.assertEqual(self.store.emit_attention_events(21), 0)
        stats = self.store.stats()
        self.assertEqual(stats['attention']['events_dead'], 0)
        self.assertEqual(stats['events']['kinds'], {'attention': 1})

    def test_expired_ack_and_fail_cannot_mutate_or_increment_counters(self):
        self.store.register_consumer('worker', 'other', 10)
        self.store.emit_event('attention', 'a', {}, 10)
        job = self.store.claim_events('worker', 11, lease_seconds=5)[0]
        for now in (16, 17):
            self.assertFalse(self.store.ack_event('worker', job['id'], now, claim_token=job['claim_token']))
            self.assertFalse(self.store.fail_event('worker', job['id'], now, 'late', claim_token=job['claim_token']))
        self.assertEqual(self.store.get_event(job['id'])['state'], 'leased')
        self.assertEqual((self.store.consumers()[0]['acks'], self.store.consumers()[0]['fails']), (0, 0))

    def test_same_consumer_reclaim_fences_old_ack_fail_and_reply(self):
        self.store.register_consumer('worker', 'other', 10)
        self.external_message()
        self.store.emit_event('attention', 'a', {}, 10)
        old = self.store.claim_events('worker', 20, lease_seconds=5)
        other = Store(self.path)
        try:
            fresh = other.claim_events('worker', 25, lease_seconds=10)
            for previous, current in zip(old, fresh):
                self.assertNotEqual(previous['claim_token'], current['claim_token'])
                before = other.get_event(current['id'])
                for token in (None, 'wrong', previous['claim_token']):
                    self.assertFalse(self.store.ack_event('worker', current['id'], 26, claim_token=token))
                    self.assertFalse(self.store.fail_event('worker', current['id'], 26, 'stale', claim_token=token))
                    self.assertFalse(self.store.complete_external_reply(
                        'worker', current['id'], 26, 'answer', reply_uuid('m1'), claim_token=token)['ok'])
                self.assertEqual(other.get_event(current['id']), before)
                if current['kind'] == 'attention':
                    self.assertTrue(other.ack_event('worker', current['id'], 26, claim_token=current['claim_token']))
                else:
                    self.assertTrue(other.complete_external_reply(
                        'worker', current['id'], 26, 'answer', reply_uuid('m1'),
                        claim_token=current['claim_token'])['ok'])
        finally:
            other.close()

    def test_live_fail_requires_owner_and_token(self):
        self.store.register_consumer('worker', 'other', 10)
        self.store.register_consumer('other', 'other', 10)
        self.store.emit_event('attention', 'a', {}, 10)
        job = self.store.claim_events('worker', 11)[0]
        self.assertFalse(self.store.ack_event('worker', job['id'], 12))
        self.assertFalse(self.store.ack_event('other', job['id'], 12, claim_token=job['claim_token']))
        self.assertFalse(self.store.fail_event('worker', job['id'], 12, 'missing'))
        self.assertFalse(self.store.fail_event('other', job['id'], 12, 'wrong owner', claim_token=job['claim_token']))
        self.assertEqual(self.store.fail_event('worker', job['id'], 12, 'retry',
                                              claim_token=job['claim_token']), 'pending')

    def test_message_ack_denied_and_timeout_still_finds_message(self):
        self.store.register_consumer('worker', 'other', 10)
        self.external_message()
        job = self.store.claim_events('worker', 20)[0]
        self.assertFalse(self.store.ack_event('worker', job['id'], 21, claim_token=job['claim_token']))
        self.assertEqual(self.store.get_message('m1')['state'], 'external_queued')
        self.assertEqual(self.store.expire_external(100, 60), 1)
        self.assertEqual(self.store.get_message('m1')['state'], 'needs_attention')

    def test_done_reply_retry_durable_exact_and_owned(self):
        self.store.register_consumer('worker', 'other', 10)
        self.store.register_consumer('other', 'other', 10)
        self.external_message()
        job = self.store.claim_events('worker', 20, lease_seconds=5)[0]
        self.assertTrue(self.store.complete_external_reply(
            'worker', job['id'], 21, 'answer', reply_uuid('m1'), claim_token=job['claim_token'])['ok'])
        event, outbox, consumers = self.store.get_event(job['id']), self.store.get_outbox_by_inbox('m1'), self.store.consumers()
        self.store.close()
        self.store = Store(self.path)
        retry = self.store.complete_external_reply(
            'worker', job['id'], 200, 'answer', reply_uuid('m1'), claim_token=job['claim_token'])
        self.assertTrue(retry['ok'])
        self.assertTrue(retry['duplicate'])
        for owner, token, body, uid in (
                ('other', job['claim_token'], 'answer', reply_uuid('m1')),
                ('worker', None, 'answer', reply_uuid('m1')),
                ('worker', 'wrong', 'answer', reply_uuid('m1')),
                ('worker', job['claim_token'], 'changed', reply_uuid('m1')),
                ('worker', job['claim_token'], 'answer', 'changed-uuid')):
            self.assertFalse(self.store.complete_external_reply(
                owner, job['id'], 200, body, uid, claim_token=token)['ok'])
        self.assertEqual(self.store.get_event(job['id']), event)
        self.assertEqual(self.store.get_outbox_by_inbox('m1'), outbox)
        self.assertEqual(self.store.consumers(), consumers)

    def test_expired_reply_rejected_before_reclaim(self):
        self.store.register_consumer('worker', 'other', 10)
        self.external_message()
        job = self.store.claim_events('worker', 20, lease_seconds=5)[0]
        self.assertFalse(self.store.complete_external_reply(
            'other', job['id'], 21, 'answer', reply_uuid('m1'), claim_token=job['claim_token'])['ok'])
        self.assertFalse(self.store.complete_external_reply(
            'worker', job['id'], 25, 'answer', reply_uuid('m1'), claim_token=job['claim_token'])['ok'])
        self.assertIsNone(self.store.get_outbox_by_inbox('m1'))

    def test_current_schema_restart_preserves_live_token_and_lease(self):
        self.store.register_consumer('worker', 'other', 10)
        self.store.emit_event('attention', 'a', {}, 10)
        job = self.store.claim_events('worker', 11, lease_seconds=5)[0]
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_event(job['id']), job)
        self.assertTrue(self.store.ack_event('worker', job['id'], 12, claim_token=job['claim_token']))

    def test_two_connection_claim_race_has_one_owner(self):
        self.store.register_consumer('worker', 'other', 10)
        self.store.emit_event('attention', 'a', {}, 10)
        other = Store(self.path)
        barrier = threading.Barrier(2)
        results, failures = [], []
        def claim(store):
            try:
                barrier.wait()
                results.extend(store.claim_events('worker', 11))
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=claim, args=(store,)) for store in (self.store, other)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
                assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
            self.assertFalse(failures)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]['claim_token'])
        finally:
            other.close()


    def test_two_different_consumer_connections_claim_one_message_once(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.store.register_consumer('cursor-main', 'other', 10)
        self.external_message()
        other = Store(self.path)
        barrier = threading.Barrier(2)
        results, failures = [], []
        def claim(store, consumer):
            try:
                barrier.wait()
                results.extend(store.claim_events(consumer, 20, limit=1, kind='message'))
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=claim, args=pair)
                   for pair in ((self.store, 'dsh-main'), (other, 'cursor-main'))]
        try:
            for thread in threads: thread.start()
            for thread in threads:
                thread.join(5)
                assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertFalse(failures)
            self.assertEqual(len(results), 1)
            winner = results[0]
            loser = 'cursor-main' if winner['consumer'] == 'dsh-main' else 'dsh-main'
            self.assertFalse(other.complete_external_reply(
                loser, winner['id'], 21, 'wrong', reply_uuid('m1'),
                claim_token=winner['claim_token'])['ok'])
        finally:
            other.close()

    def test_external_app_daily_budget_serializes_competing_publishers(self):
        self.store.register_consumer('dsh-main', 'dsh', 10)
        self.store.register_consumer('cursor-main', 'other', 10)
        for mid in ('m1', 'm2'):
            self.external_message(mid, sender_type='app')
        first = self.store.claim_events('dsh-main', 20, limit=1)[0]
        other = Store(self.path)
        second = other.claim_events('cursor-main', 20, limit=1)[0]
        barrier = threading.Barrier(2)
        results, failures = [], []
        def publish(store, event, mid):
            try:
                barrier.wait()
                results.append((mid, store.complete_external_reply(
                    event['consumer'], event['id'], 21, 'body', reply_uuid(mid),
                    claim_token=event['claim_token'], app_daily_cap=1)))
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=publish, args=args) for args in
                   ((self.store, first, 'm1'), (other, second, 'm2'))]
        try:
            for thread in threads: thread.start()
            for thread in threads:
                thread.join(5)
                assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertFalse(failures)
            self.assertEqual(sum(result['ok'] for _, result in results), 1)
            rejected = next(mid for mid, result in results if not result['ok'])
            self.assertIsNone(self.store.get_outbox_by_inbox(rejected))
            self.assertEqual(self.store.get_message(rejected)['state'], 'needs_attention')
            rejected_event = first if rejected == 'm1' else second
            self.assertEqual(self.store.get_event(rejected_event['id'])['state'], 'dead')
            self.assertIn('budget', next(result['reason'] for mid, result in results if mid == rejected))
            accepted = second if rejected == 'm1' else first
            accepted_mid = 'm2' if rejected == 'm1' else 'm1'
            duplicate = self.store.complete_external_reply(
                accepted['consumer'], accepted['id'], 22, 'body', reply_uuid(accepted_mid),
                claim_token=accepted['claim_token'], app_daily_cap=0)
            self.assertTrue(duplicate['ok'])
            self.assertTrue(duplicate['duplicate'])
        finally:
            other.close()



    def test_shared_app_budget_preserves_rolling_and_lifetime_caps(self):
        self.store.register_consumer('worker', 'other', 10)
        for index in range(3):
            mid = 'app-' + str(index)
            self.external_message(mid, sender_type='app')
            event = self.store.claim_events('worker', 20 + index, limit=1)[0]
            self.assertTrue(self.store.complete_external_reply(
                'worker', event['id'], 21 + index, 'body', reply_uuid(mid),
                claim_token=event['claim_token'])['ok'])
        self.external_message('fourth', sender_type='app')
        job = self.store.get_message('fourth')
        self.assertTrue(self.store.app_reply_blocked(job, 24))
        self.assertFalse(self.store.app_reply_blocked(job, 1000))
        self.assertTrue(self.store.app_reply_blocked(job, 1000, daily_cap=3))
        self.assertFalse(self.store.app_reply_blocked(dict(job, sender_type='user'), 24, daily_cap=0))
        fourth = self.store.claim_events('worker', 24, limit=1)[0]
        self.assertFalse(self.store.complete_external_reply(
            'worker', fourth['id'], 24, 'body', reply_uuid('fourth'),
            claim_token=fourth['claim_token'])['ok'])
        self.assertIsNone(self.store.get_outbox_by_inbox('fourth'))
        for mid in ('thread-1', 'thread-2'):
            self.external_message(mid, sender_type='app', thread_id='root')
            event = self.store.claim_events('worker', 1000, limit=1)[0]
            self.assertTrue(self.store.complete_external_reply(
                'worker', event['id'], 1000, 'body', reply_uuid(mid),
                claim_token=event['claim_token'])['ok'])
        self.external_message('thread-3', sender_type='app', thread_id='root')
        self.assertTrue(self.store.app_reply_blocked(self.store.get_message('thread-3'), 100000))
        third = self.store.claim_events('worker', 100000, limit=1)[0]
        self.assertFalse(self.store.complete_external_reply(
            'worker', third['id'], 100000, 'body', reply_uuid('thread-3'),
            claim_token=third['claim_token'])['ok'])
        self.assertIsNone(self.store.get_outbox_by_inbox('thread-3'))


class MigrationTests(unittest.TestCase):
    def legacy_database(self, path, version):
        db = sqlite3.connect(str(path), isolation_level=None)
        db.executescript('''
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES ('service_epoch','12345');
            CREATE TABLE inbox(id TEXT PRIMARY KEY,state TEXT NOT NULL DEFAULT 'details_pending',
                received_at REAL NOT NULL,next_due REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                attempt_id TEXT,lease_until REAL,claim_version TEXT,chat_id TEXT,parent_id TEXT,root_id TEXT,
                thread_id TEXT,create_ms INTEGER,version TEXT,raw_json TEXT,text TEXT,sender_type TEXT,sender_id TEXT,error TEXT);
            INSERT INTO inbox(id,state,received_at,attempts,chat_id) VALUES ('m1','completed',10,4,'oc_legacy');
            CREATE TABLE outbox(id TEXT PRIMARY KEY REFERENCES inbox(id),original_id TEXT NOT NULL UNIQUE REFERENCES inbox(id),
                uuid TEXT NOT NULL UNIQUE,body TEXT NOT NULL,body_hash TEXT NOT NULL,created_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',next_due REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                attempt_id TEXT,lease_until REAL,first_send_at REAL,reply_id TEXT,sent_at REAL,verified_at REAL,error TEXT);
            INSERT INTO outbox(id,original_id,uuid,body,body_hash,created_at,state,attempts,reply_id)
                VALUES ('m1','m1','uuid','old body','hash',10,'completed',7,'reply');
        ''')
        if version == 2:
            db.executescript('''
                CREATE TABLE consumers(name TEXT PRIMARY KEY,kind TEXT NOT NULL,created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,claims INTEGER NOT NULL DEFAULT 0,acks INTEGER NOT NULL DEFAULT 0,
                    fails INTEGER NOT NULL DEFAULT 0);
                INSERT INTO consumers VALUES ('worker','other',10,10,3,1,0);
                CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,dedupe_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,created_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,lease_until REAL,consumer TEXT,claimed_at REAL,acked_at REAL,
                    next_due REAL NOT NULL DEFAULT 0,error TEXT);
                INSERT INTO events(kind,dedupe_key,payload,created_at,state,attempts,lease_until,consumer,claimed_at)
                    VALUES ('attention','a','{}',10,'leased',3,999,'worker',11);
                INSERT INTO events(kind,dedupe_key,payload,created_at,state,attempts,consumer,acked_at)
                    VALUES ('attention','done','{}',10,'done',1,'worker',12);
            ''')
        db.execute('PRAGMA user_version=%d' % version)
        db.close()

    def test_v1_and_v2_upgrade_preserves_records_and_invalidates_untokened_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            for version in (1, 2):
                with self.subTest(version=version):
                    path = Path(temp) / ('v%d.sqlite3' % version)
                    self.legacy_database(path, version)
                    store = Store(path)
                    try:
                        self.assertEqual(
                            store.connection.execute('PRAGMA user_version').fetchone()[0], Store.SCHEMA_VERSION)
                        self.assertEqual(store.meta('service_epoch'), 12345)
                        self.assertEqual((store.get_message('m1')['state'], store.get_message('m1')['attempts']), ('completed', 4))
                        row = store.get_outbox_by_inbox('m1')
                        self.assertEqual((row['body'], row['state'], row['attempts'], row['verify_attempts']),
                                         ('old body', 'completed', 7, 0))
                        if version == 2:
                            event = store.get_event(1)
                            self.assertEqual((event['state'], event['attempts'], event['lease_until']), ('pending', 3, None))
                            self.assertIsNone(event['claim_token'])
                            self.assertEqual(store.get_event(2)['state'], 'done')
                            self.assertEqual(store.consumers()[0]['claims'], 3)
                            self.assertTrue(store.claim_events('worker', 20)[0]['claim_token'])
                    finally:
                        store.close()

    def test_v4_renames_the_legacy_analysis_completed_state(self):
        # L0：内置 analyst 删除，'analysis_completed' 说的那一步不存在了；
        # 语义不变（回复已写进 outbox、等 sender 发出），只把状态名改成它真正的含义。
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'v3.sqlite3'
            self.legacy_database(path, 2)
            raw = sqlite3.connect(str(path), isolation_level=None)
            raw.execute("ALTER TABLE events ADD COLUMN claim_token TEXT")
            raw.execute("ALTER TABLE outbox ADD COLUMN verify_attempts INTEGER NOT NULL DEFAULT 0")
            raw.execute("UPDATE inbox SET state='analysis_completed' WHERE id='m1'")
            raw.execute('PRAGMA user_version=3')
            raw.close()
            store = Store(path)
            try:
                self.assertEqual(store.get_message('m1')['state'], 'reply_queued')
                self.assertEqual(
                    store.connection.execute('PRAGMA user_version').fetchone()[0], Store.SCHEMA_VERSION)
            finally:
                store.close()

    def test_failed_migration_rolls_back_columns_and_lease_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.sqlite3'
            self.legacy_database(path, 2)
            raw = sqlite3.connect(str(path), isolation_level=None)
            raw.execute("CREATE TRIGGER block_migration BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'blocked'); END")
            before = list(raw.iterdump())
            with self.assertRaises(sqlite3.IntegrityError):
                Store(path)
            self.assertEqual(list(raw.iterdump()), before)
            self.assertEqual(raw.execute('PRAGMA user_version').fetchone()[0], 2)
            raw.close()

    def test_future_schema_rejected_without_modifying_file_or_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'future.sqlite3'
            raw = sqlite3.connect(str(path))
            raw.execute('CREATE TABLE future_only(value TEXT)')
            raw.execute("INSERT INTO future_only VALUES ('keep')")
            raw.execute('PRAGMA user_version=99')
            raw.commit()
            raw.close()
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, '99'):
                Store(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + '-wal').exists())
            self.assertFalse(Path(str(path) + '-shm').exists())


if __name__ == '__main__':
    unittest.main()
