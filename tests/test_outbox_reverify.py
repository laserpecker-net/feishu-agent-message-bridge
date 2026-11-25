"""被误判的终回复怎么修回来：``feishu-bridge outbox reverify`` 的判据。

判据能直接重跑：``python3 -m pytest tests/test_outbox_reverify.py -q``。
原则：**重判确实通过才迁移状态**；正文真对不上的、没有 reply_id 的、别的 needs_attention
一律原样留着，可重跑、幂等。
"""

import json
import tempfile
import unittest
from pathlib import Path

from bridge import reverify
from bridge.store import Store

ORIGINAL = {'message_id': 'om_1', 'chat_id': 'oc_1'}
WITH_AT = '<at user_id="ou_1"></at> 收到，<at user_id="ou_2"></at> 你看下'


def message(text, mentions=None, chat_id='oc_1', parent='om_1', sender='ou_me'):
    reply = {'chat_id': chat_id, 'parent_id': parent, 'sender': {'id': sender},
             'body': {'content': json.dumps({'text': text}, ensure_ascii=False)}}
    if mentions is not None:
        reply['mentions'] = [{'id': item} if isinstance(item, str) else item for item in mentions]
    return reply


class ReverifyCases(unittest.TestCase):

    """历史误判行的修复：能修好的才修，真的对不上的原样留着。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.replies = {}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def seed(self, mid, body, reply_text, mentions=None, reply_id='om_r', error='Reply readback mismatch; no resend'):
        self.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,version,raw_json)'
            ' VALUES(?,?,?,0,?,?,?,?)',
            (mid, 'needs_attention', 10.0, 'oc_1', 'user', 'v1',
             json.dumps(ORIGINAL, ensure_ascii=False)))
        cursor = self.store.connection.execute(
            'INSERT INTO outbox(inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,created_at,'
            'next_due,state,reply_id,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, 'oc_1', 1, 'reply', body, '', None, 'u-%s' % mid, 10.0, 10.0,
             'needs_attention', reply_id, error))
        self.store.connection.commit()
        if reply_id is not None:
            self.replies[reply_id] = message(reply_text, mentions=mentions)
        return cursor.lastrowid

    def transport(self):
        replies = self.replies

        class Fake:
            def get_message(self, mid):
                return replies[mid]
        return Fake()

    def reverify(self, **kwargs):
        return reverify.reverify(self.store, self.transport(), 'ou_me', now=20.0, **kwargs)

    def test_mention_rows_are_fixed_and_inbox_returns_to_completed(self):
        row_id = self.seed('om_1', WITH_AT, '@张三 收到，@李四 你看下', mentions=['ou_1', 'ou_2'])
        results = self.reverify()
        self.assertEqual([(item['row_id'], item['ok']) for item in results], [(row_id, True)])
        row = self.store.get_outbox(row_id)
        self.assertEqual(row['state'], 'reply_verified')
        self.assertIsNone(row['error'])
        self.assertEqual(self.store.get_message('om_1')['state'], 'completed')

    def test_truly_mismatched_row_stays_untouched(self):
        row_id = self.seed('om_2', 'answer', 'different answer')
        results = self.reverify()
        self.assertFalse(results[0]['ok'])
        self.assertEqual(self.store.get_outbox(row_id)['state'], 'needs_attention')

    def test_row_without_reply_id_is_not_a_candidate(self):
        self.seed('om_3', WITH_AT, '', reply_id=None)
        self.assertEqual(self.reverify(), [])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE state='needs_attention'").fetchone()[0], 1)

    def test_dry_run_reports_without_writing(self):
        row_id = self.seed('om_4', WITH_AT, '@张三 收到，@李四 你看下', mentions=['ou_1', 'ou_2'])
        results = self.reverify(dry_run=True)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(self.store.get_outbox(row_id)['state'], 'needs_attention')

    def test_second_run_is_idempotent(self):
        self.seed('om_5', WITH_AT, '@张三 收到，@李四 你看下', mentions=['ou_1', 'ou_2'])
        self.assertEqual(len(self.reverify()), 1)
        self.assertEqual(self.reverify(), [])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE state='reply_verified'").fetchone()[0], 1)

    def test_unrelated_needs_attention_is_never_rewritten(self):
        """只有那两类「判据错了」的错误能走这个口子，别的错误原样留着。"""
        row_id = self.seed('om_6', 'answer', 'answer', error='Reply row has no source message')
        self.assertEqual(self.reverify(), [])
        self.assertEqual(self.store.get_outbox(row_id)['state'], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
