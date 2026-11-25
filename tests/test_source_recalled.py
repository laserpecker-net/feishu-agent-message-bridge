"""来源被撤回/改写时，那条回复必须落**明确终态**，而不是挂进「待人工」。

判据能直接重跑：``python3 -m pytest tests/test_source_recalled.py -q``

为什么这条边界值得单独钉：消息被撤回之后，人**没有任何可做的事**。以前它走
``fail_outbox(permanent=True)`` → ``needs_attention`` + 一条待办事件，于是 ``doctor`` 永远
warn，而每次人工都要重新判断一遍「这条是不是早就没救了」。待办的定义是「人还能做点什么」，
做不到的事就该落终态。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bridge.service import Service
from bridge.transport import message_version


class SourceTransport:
    def __init__(self, fresh):
        self.fresh = fresh
        self.sent = []

    def get_message(self, mid):
        return dict(self.fresh)

    def reply(self, original, body, uuid, before_send=None):
        if before_send:
            before_send()
        self.sent.append(body)
        return 'om_reply'


class SourceRecalledCases(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        config = {'target_open_id': 'ou_target', 'sending_enabled': True, 'agent_timeout': 60,
                  'cursor_binary': '/fake', 'lark_binary': '/fake'}
        (home / 'config.json').write_text(json.dumps(config))
        self.service = Service(home, config)
        self.service.authenticated.set()
        self.addCleanup(self.service.store.close)
        self.store = self.service.store

    def seed(self, fresh, inbox_state='queued'):
        """按「detail 读到的原始消息」建账：inbox 的 version 就是那一刻的版本号。"""
        version = message_version(fresh)
        self.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,next_due,chat_id,sender_type,version,'
            'raw_json,text) VALUES(?,?,?,0,?,?,?,?,?)',
            ('om_in', inbox_state, 10.0, 'oc_1', 'user', version, json.dumps(fresh), '原始消息'))
        cursor = self.store.connection.execute(
            'INSERT INTO outbox(inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,'
            'created_at,next_due,state,attempt_id,lease_until)'
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('om_in', 'oc_1', 1, 'reply', '收到，我看下', 'h', None, 'u1', 10.0, 10.0,
             'send_started', 'a1', time.time() + 600))
        self.store.connection.commit()
        return cursor.lastrowid

    def outgoing(self, row_id):
        return {'row_id': row_id, 'attempt_id': 'a1', 'inbox_id': 'om_in', 'mode': 'send',
                'body': '收到，我看下', 'uuid': 'u1', 'reply_id': None, 'chat_id': 'oc_1'}

    def attention_counts(self):
        return {
            'inbox': self.store.connection.execute(
                "SELECT COUNT(*) FROM inbox WHERE state='needs_attention'").fetchone()[0],
            'outbox': self.store.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE state='needs_attention'").fetchone()[0],
            'events': self.store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE kind='attention'").fetchone()[0],
        }

    def test_recalled_source_drops_the_reply_and_creates_no_todo(self):
        original = {'message_id': 'om_in', 'chat_id': 'oc_1',
                    'body': {'content': json.dumps({'text': '原始消息'})}}
        row_id = self.seed(original)
        self.service.transport = SourceTransport(dict(original, deleted=True))
        self.service.send_reply_row(self.outgoing(row_id))

        row = self.store.connection.execute(
            'SELECT * FROM outbox WHERE row_id=?', (row_id,)).fetchone()
        self.assertEqual(row['state'], 'dropped')
        self.assertEqual(row['error'], 'Source message recalled or edited; reply dropped')
        inbox = self.store.connection.execute(
            'SELECT * FROM inbox WHERE id=?', ('om_in',)).fetchone()
        self.assertEqual(inbox['state'], 'completed')
        self.assertEqual(inbox['error'], 'source recalled; nothing to send')
        self.assertEqual(self.attention_counts(), {'inbox': 0, 'outbox': 0, 'events': 0})
        self.assertEqual(self.service.transport.sent, [])

    def test_edited_source_drops_the_reply_too(self):
        original = {'message_id': 'om_in', 'chat_id': 'oc_1',
                    'body': {'content': json.dumps({'text': '原始消息'})}}
        row_id = self.seed(original)
        edited = dict(original, body={'content': json.dumps({'text': '改过的消息'})})
        self.service.transport = SourceTransport(edited)
        self.service.send_reply_row(self.outgoing(row_id))

        self.assertEqual(self.store.connection.execute(
            'SELECT state FROM outbox WHERE row_id=?', (row_id,)).fetchone()['state'], 'dropped')
        self.assertEqual(self.attention_counts()['outbox'], 0)

    def test_unchanged_source_still_sends(self):
        original = {'message_id': 'om_in', 'chat_id': 'oc_1',
                    'body': {'content': json.dumps({'text': '原始消息'})}}
        row_id = self.seed(original)
        self.service.transport = SourceTransport(original)
        with mock.patch.object(self.service, 'replyable_chats', return_value={'oc_1'}):
            self.service.send_reply_row(self.outgoing(row_id))

        self.assertEqual(self.service.transport.sent, ['收到，我看下'])
        row = self.store.connection.execute(
            'SELECT state,reply_id FROM outbox WHERE row_id=?', (row_id,)).fetchone()
        self.assertEqual(row['state'], 'verify_pending')
        self.assertEqual(row['reply_id'], 'om_reply')
        self.assertEqual(self.attention_counts(), {'inbox': 0, 'outbox': 0, 'events': 0})

    def test_drop_outbox_refuses_a_stale_attempt(self):
        original = {'message_id': 'om_in', 'chat_id': 'oc_1',
                    'body': {'content': json.dumps({'text': '原始消息'})}}
        row_id = self.seed(original)
        self.assertFalse(self.store.drop_outbox(row_id, 'other-attempt', time.time(),
                                                'Source message recalled or edited; reply dropped',
                                                'source recalled; nothing to send'))
        self.assertEqual(self.store.connection.execute(
            'SELECT state FROM outbox WHERE row_id=?', (row_id,)).fetchone()['state'],
            'send_started')


if __name__ == '__main__':
    unittest.main()
