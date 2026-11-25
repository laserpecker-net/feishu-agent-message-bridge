"""待人工项的关单出口：``outbox reverify`` 顺手关待办、``attention list/close``。

判据能直接重跑：``python3 -m pytest tests/test_attention_close.py -q``。

三条不许松的口子：
1. 修好一行 → 它的配套待办必须**同事务**关掉（否则账本永远挂着僵尸）；
2. 没修好的行、来源仍是 needs_attention 的待办 → **不许**借清理的口子消失；
3. 关单只改状态、必须留理由、**不伪造回复**（不写 outbox、不改 reply_id）。
"""

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bridge import attention, cli, reverify
from bridge.store import Store

ORIGINAL = {'message_id': 'om_1', 'chat_id': 'oc_1'}
REPLY_TEXT = '收到，我看下'


def reply_message(text=REPLY_TEXT, chat_id='oc_1', parent='om_1', sender='ou_me'):
    return {'chat_id': chat_id, 'parent_id': parent, 'sender': {'id': sender},
            'body': {'content': json.dumps({'text': text}, ensure_ascii=False)}}


class AttentionCases(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.replies = {}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # --- 造数据 ---------------------------------------------------------------

    def seed(self, mid, inbox_state='needs_attention', reply_error='Reply readback mismatch; no resend',
             with_attention=True, reply_id='om_r', text='原始消息', reply_text=REPLY_TEXT):
        self.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,version,raw_json,text)'
            ' VALUES(?,?,?,0,?,?,?,?,?)',
            (mid, inbox_state, 10.0, 'oc_1', 'user', 'v1', json.dumps(ORIGINAL), text))
        row_id = None
        if reply_error is not None:
            cursor = self.store.connection.execute(
                'INSERT INTO outbox(inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,'
                'created_at,next_due,state,reply_id,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (mid, 'oc_1', 1, 'reply', REPLY_TEXT, '', None, 'u-%s' % mid, 10.0, 10.0,
                 'needs_attention', reply_id, reply_error))
            row_id = cursor.lastrowid
        if with_attention:
            self.store.connection.execute(
                'INSERT INTO events(kind,dedupe_key,payload,created_at,state) VALUES(?,?,?,?,?)',
                ('attention', 'attention:' + mid,
                 json.dumps({'message_id': mid, 'error': reply_error}), 11.0, 'pending'))
        self.store.connection.commit()
        self.replies[mid] = ORIGINAL
        if reply_id:
            self.replies[reply_id] = reply_message(text=reply_text)
        return row_id

    def event_row(self, mid):
        return self.store.connection.execute(
            'SELECT * FROM events WHERE dedupe_key=?', ('attention:' + mid,)).fetchone()

    def inbox_row(self, mid):
        return self.store.connection.execute('SELECT * FROM inbox WHERE id=?', (mid,)).fetchone()

    def transport(self):
        replies = self.replies

        class Fake:
            def get_message(self, mid):
                return replies[mid]
        return Fake()

    def reverify(self, **kwargs):
        return reverify.reverify(self.store, self.transport(), 'ou_me', now=20.0, **kwargs)

    # --- A. 修好一行就关掉它的待办 ------------------------------------------

    def test_reverify_closes_the_matching_attention_event(self):
        self.seed('om_1')
        results = self.reverify()
        self.assertTrue(results[0]['ok'])
        row = self.event_row('om_1')
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['error'], 'auto-closed: reply verified on reverify')
        self.assertEqual(row['acked_at'], 20.0)
        self.assertEqual(self.inbox_row('om_1')['state'], 'completed')

    def test_unfixed_row_keeps_its_attention_event(self):
        """重判不通过的行，待办必须还在——修不干净就还它一个待办，不装干净。"""
        # 行的错误类别属于「可重判」，但重判本身不通过（回读正文对不上）。
        self.seed('om_1', reply_text='完全不同的内容')
        results = self.reverify()
        self.assertFalse(results[0]['ok'])
        self.assertEqual(self.event_row('om_1')['state'], 'pending')

    def test_dry_run_closes_nothing(self):
        self.seed('om_1')
        self.reverify(dry_run=True)
        self.assertEqual(self.event_row('om_1')['state'], 'pending')
        self.assertEqual(self.inbox_row('om_1')['state'], 'needs_attention')

    # --- A2. --close-stale 只碰「来源已完成」的 ----------------------------

    def test_close_stale_only_closes_events_whose_source_is_completed(self):
        self.seed('om_done', inbox_state='completed', reply_error=None)
        self.seed('om_still_bad', inbox_state='needs_attention', reply_error=None)
        result = reverify.close_stale(self.store, now=30.0)
        self.assertEqual(result['closed'], 1)
        self.assertEqual(self.event_row('om_done')['state'], 'done')
        self.assertEqual(self.event_row('om_done')['error'],
                         'auto-closed: source inbox already completed')
        self.assertEqual(self.event_row('om_still_bad')['state'], 'pending')

    def test_close_stale_is_idempotent(self):
        self.seed('om_done', inbox_state='completed', reply_error=None)
        self.assertEqual(reverify.close_stale(self.store, now=30.0)['closed'], 1)
        self.assertEqual(reverify.close_stale(self.store, now=31.0)['closed'], 0)

    def test_close_stale_dry_run_reports_without_writing(self):
        self.seed('om_done', inbox_state='completed', reply_error=None)
        result = reverify.close_stale(self.store, now=30.0, dry_run=True)
        self.assertEqual(result['closed'], 1)
        self.assertEqual(self.event_row('om_done')['state'], 'pending')

    def test_format_stale_says_how_many_closed_and_left(self):
        self.seed('om_done', inbox_state='completed', reply_error=None)
        result = reverify.close_stale(self.store, now=30.0)
        text = reverify.format_stale(result)
        self.assertIn('已关掉 1 条', text)
        self.assertIn('还剩 0 条', text)

    # --- B. attention list ----------------------------------------------------

    def test_list_shows_three_kinds_with_counts(self):
        self.seed('om_1')
        self.seed('om_2', inbox_state='completed', reply_error=None)
        rows = attention.items(self.store)
        # 三类：待人工消息、待人工出站行、待办事件。出站行排在事件前面（同一批人关单时更好对照）。
        self.assertEqual([row['ref'] for row in rows],
                         ['inbox:om_1', 'outbox:1', 'event:1', 'event:2'])
        table = attention.format_table(rows)
        self.assertIn('inbox:om_1', table)
        self.assertIn('outbox:1', table)
        self.assertIn('event:2', table)
        self.assertIn('待处理 inbox 1 条 / outbox 1 条 / 事件 2 条', table)

    def test_list_includes_outbox_attention_rows_with_preview(self):
        row_id = self.seed('om_1')
        rows = [row for row in attention.items(self.store) if row['kind'] == '出站']
        self.assertEqual([row['ref'] for row in rows], ['outbox:%d' % row_id])
        self.assertIn('Reply readback', rows[0]['reason'])
        self.assertIn('收到，我看下', rows[0]['preview'])

    def test_close_outbox_drops_row_and_completes_source(self):
        row_id = self.seed('om_1')
        result = attention.close(self.store, 'outbox:%d' % row_id, '来源被撤回了，不发', now=40.0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['before'], 'needs_attention')
        self.assertEqual(result['after'], 'dropped')
        self.assertEqual(result['events_closed'], 1)
        outbox = self.store.connection.execute(
            'SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
        self.assertEqual(outbox['state'], 'dropped')
        self.assertEqual(outbox['error'], 'closed by operator: 来源被撤回了，不发')
        # 来源消息必须一起落终态，否则 attention list 会再列一次同一条消息、永远收敛不了。
        inbox = self.inbox_row('om_1')
        self.assertEqual(inbox['state'], 'completed')
        self.assertEqual(inbox['error'], 'no reply will be sent: 来源被撤回了，不发')
        self.assertEqual(self.event_row('om_1')['state'], 'done')

    def test_close_outbox_is_idempotent_and_never_fakes_a_reply(self):
        row_id = self.seed('om_1')
        before = self.store.connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        self.assertTrue(attention.close(self.store, 'outbox:%d' % row_id, '看过了', now=40.0)['ok'])
        again = attention.close(self.store, 'outbox:%d' % row_id, '再看一次', now=41.0)
        self.assertFalse(again['ok'])
        self.assertIn('不是待人工状态', again['error'])
        after = self.store.connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        self.assertEqual(before, after)
        # 不伪造回复：reply_id 原样不动（关单只改状态）。
        self.assertEqual(self.store.connection.execute(
            'SELECT reply_id FROM outbox WHERE row_id=?', (row_id,)).fetchone()['reply_id'], 'om_r')

    def test_dead_events_of_any_kind_can_be_listed_and_closed(self):
        self.seed('om_1')
        self.store.connection.execute(
            'INSERT INTO events(kind,dedupe_key,payload,created_at,state,error) VALUES(?,?,?,?,?,?)',
            ('message', 'message:om_dead', json.dumps({'message_id': 'om_dead'}), 12.0, 'dead',
             'Application reply-loop budget reached'))
        rows = [row for row in attention.items(self.store) if row['kind'] == '待办']
        targets = {row['ref']: row for row in rows}
        dead_ref = [ref for ref in targets if ref != 'event:1'][0]
        self.assertIn('reply-loop budget', targets[dead_ref]['reason'])
        result = attention.close(self.store, dead_ref, '已知的一次循环，不再重试', now=44.0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['after'], 'done')
        row = self.store.connection.execute(
            'SELECT state,error FROM events WHERE dedupe_key=?', ('message:om_dead',)).fetchone()
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['error'], 'closed by operator: 已知的一次循环，不再重试')

    def test_doctor_attention_clear_counts_outbox(self):
        from bridge.cli import _doctor_checks
        self.seed('om_1')
        report = {'pid': 1, 'heartbeat_age_seconds': 1,
                  'identity': {'userName': 'tester', 'available': True},
                  'attention': self.store.stats()['attention']}
        checks = {check['name']: check for check in
                  _doctor_checks(report, {}, {}, True, None)}
        self.assertEqual(checks['attention_clear']['status'], 'warn')
        self.assertIn('outbox_needs_attention=1', checks['attention_clear']['detail'])
        attention.close(self.store, 'inbox:om_1', '看过了', now=40.0)
        attention.close(self.store, 'outbox:1', '看过了', now=40.0)
        attention.close(self.store, 'event:1', '看过了', now=40.0)
        attention.close(self.store, 'event:2', '看过了', now=40.0)
        report['attention'] = self.store.stats()['attention']
        checks = {check['name']: check for check in _doctor_checks(report, {}, {}, True, None)}
        self.assertEqual(checks['attention_clear']['status'], 'ok')

    # --- B2. close ------------------------------------------------------------

    def test_close_inbox_marks_completed_with_reason_and_closes_its_event(self):
        self.seed('om_1')
        result = attention.close(self.store, 'inbox:om_1', '看过了，不值得再管', now=40.0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['before'], 'needs_attention')
        self.assertEqual(result['after'], 'completed')
        self.assertEqual(result['events_closed'], 1)
        row = self.inbox_row('om_1')
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['error'], 'dismissed by operator: 看过了，不值得再管')
        self.assertEqual(self.event_row('om_1')['state'], 'done')

    def test_close_event_marks_done_and_leaves_source_alone(self):
        self.seed('om_1', inbox_state='needs_attention', reply_error=None)
        result = attention.close(self.store, 'event:1', '原因已经清楚了', now=40.0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['before'], 'pending')
        self.assertEqual(result['after'], 'done')
        row = self.event_row('om_1')
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['acked_at'], 40.0)
        self.assertEqual(row['error'], 'closed by operator: 原因已经清楚了')
        # 关待办不替来源消息「结案」：它还挂着 needs_attention，人自己决定。
        self.assertEqual(self.inbox_row('om_1')['state'], 'needs_attention')

    def test_close_never_fakes_a_reply(self):
        self.seed('om_1')
        before = self.store.connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        attention.close(self.store, 'inbox:om_1', '看过了', now=40.0)
        after = self.store.connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(self.store.connection.execute(
            'SELECT reply_id FROM outbox WHERE inbox_id=?', ('om_1',)).fetchone()['reply_id'], 'om_r')

    def test_bad_ref_is_refused(self):
        self.assertFalse(attention.close(self.store, 'om_1', '看过了')['ok'])
        self.assertFalse(attention.close(self.store, 'event:abc', '看过了')['ok'])
        self.assertFalse(attention.close(self.store, 'inbox:', '看过了')['ok'])

    def test_unknown_or_terminal_refs_are_refused(self):
        self.seed('om_1')
        self.assertFalse(attention.close(self.store, 'inbox:om_missing', '看过了')['ok'])
        self.assertTrue(attention.close(self.store, 'inbox:om_1', '看过了', now=40.0)['ok'])
        second = attention.close(self.store, 'inbox:om_1', '再看一次', now=41.0)
        self.assertFalse(second['ok'])
        self.assertIn('不是待人工状态', second['error'])

    # --- B3. CLI 退出码 -------------------------------------------------------

    def run_cli(self, *argv):
        buffer = io.StringIO()
        full = ['feishu-bridge', '--home', str(self.home)] + list(argv)
        with mock.patch.object(sys, 'argv', full), contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        return caught.exception.code, buffer.getvalue()

    def test_cli_list_prints_refs(self):
        self.seed('om_1')
        code, out = self.run_cli('attention', 'list')
        self.assertEqual(code, 0)
        self.assertIn('inbox:om_1', out)

    def test_cli_close_requires_a_note(self):
        self.seed('om_1')
        code, out = self.run_cli('attention', 'close', 'inbox:om_1', '--note', '   ')
        self.assertEqual(code, 2)
        self.assertIn('--note', out)
        self.assertEqual(self.inbox_row('om_1')['state'], 'needs_attention')

    def test_cli_close_success_and_failure_exit_codes(self):
        self.seed('om_1')
        code, out = self.run_cli('attention', 'close', 'inbox:om_1', '--note', '看过了')
        self.assertEqual(code, 0)
        self.assertIn('needs_attention → completed', out)
        code, out = self.run_cli('attention', 'close', 'event:999', '--note', '看过了')
        self.assertEqual(code, 1)
        self.assertIn('找不到', out)
        code, out = self.run_cli('attention', 'close', 'nonsense', '--note', '看过了')
        self.assertEqual(code, 1)
        self.assertIn('格式不对', out)

    def test_attention_clear_becomes_ok_after_closing_everything(self):
        self.seed('om_1')
        self.seed('om_2', inbox_state='completed', reply_error=None)
        attention.close(self.store, 'inbox:om_1', '看过了', now=40.0)
        attention.close(self.store, 'event:2', '看过了', now=40.0)
        stats = self.store.stats()['attention']
        self.assertEqual(stats['inbox_needs_attention'], 0)
        self.assertEqual(stats['events_dead'], 0)


if __name__ == '__main__':
    unittest.main()
