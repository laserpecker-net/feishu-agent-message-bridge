"""说明书：触发判定、切分、唯一出口、幂等、不进教材。

对应需求：自己跟自己私聊里 @ 自己发 help → 桥完整回一份使用说明书；
终端 `feishu-bridge help` 打印同一份正文（正文只在 ``bridge/help.py`` 定义一次）。
"""

from tests.identity_doubles import UserModeDouble
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

from bridge import help as help_manual
from bridge.cli import _manual
from bridge.service import Service

OWNER = 'ou_owner'
CHAT = 'oc_selfchat'


def message(text='help', *, chat=CHAT, sender=OWNER, sender_type='user',
            mention=True, msg_type='text', mid='om_help'):
    body = {'text': text} if msg_type == 'text' else {'title': '', 'content': [[{'tag': 'text', 'text': text}]]}
    return {
        'message_id': mid, 'chat_id': chat, 'msg_type': msg_type,
        'sender': {'id': sender, 'sender_type': sender_type},
        'mentions': [{'id': OWNER, 'name': '本人'}] if mention else [],
        'body': {'content': json.dumps(body, ensure_ascii=False)},
        'create_time': str(int(time.time() * 1000)), 'deleted': False,
    }


class FakeTransport(UserModeDouble):
    """只回答一条消息；不发真网络。"""

    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    def context(self, message):
        return []

    def probe_group(self, chat):
        pass

    def inventory(self):
        return []

    def get_message(self, mid):
        return self.payload

    def send_text(self, chat_id, body, dedupe_uuid=None):
        self.sent.append((chat_id, body, dedupe_uuid))
        return 'om_sent_%d' % len(self.sent)

    def reply(self, mid, body, dedupe_uuid=None):
        self.sent.append((mid, body, dedupe_uuid))
        return 'om_sent_%d' % len(self.sent)



FakeTransport.owner_id = OWNER

def build_service(payload):
    folder = tempfile.TemporaryDirectory()
    home = Path(folder.name)
    config = {'target_open_id': OWNER, 'sending_enabled': True, 'agent_timeout': 60,
              'cursor_binary': '/fake', 'lark_binary': '/fake', 'self_chat_ids': [CHAT]}
    (home / 'config.json').write_text(json.dumps(config))
    service = Service(home, config)
    service.transport = FakeTransport(payload)
    service.authenticated.set()
    service.store.set_meta('service_epoch_ms', int(time.time() * 1000) - 86400000)
    return folder, service


class TriggerTests(unittest.TestCase):
    def wants(self, msg):
        return help_manual.wants(msg, owner_id=OWNER, self_chat_ids={CHAT})

    def test_help_word_hits(self):
        # 命令只有一个词：help（大小写不敏感）。「帮助」「怎么用」这类近似说法
        # 不再触发说明书——它们由 bridge.commands 回一句"命令要精确"（见 test_commands.py）。
        for word in ('help', 'HELP', 'Help'):
            with self.subTest(word=word):
                self.assertTrue(self.wants(message(word)))
        self.assertTrue(self.wants(message('@_user_1 help！')))

    def test_vague_words_do_not_open_the_manual(self):
        for word in ('帮助', '怎么用', '说明书'):
            with self.subTest(word=word):
                self.assertFalse(self.wants(message(word)))

    def test_missing_self_mention_does_not_hit(self):
        self.assertFalse(self.wants(message('help', mention=False)))

    def test_group_chat_does_not_hit(self):
        self.assertFalse(self.wants(message('help', chat='oc_some_group')))
        # 一对一私聊：会话不在白名单里也认（主人自己的 DM 会话常常不在白名单）。
        other = message('help', chat='oc_a_private_dm')
        self.assertFalse(self.wants(other))
        self.assertTrue(help_manual.wants(other, owner_id=OWNER, self_chat_ids={CHAT},
                                          allow_any_private=True))
        # 一对一私聊不必 @ 自己：直接打「help」也认（只可能是本人发的）。
        self.assertTrue(help_manual.wants(message('help', chat='oc_a_private_dm', mention=False),
                                          owner_id=OWNER, self_chat_ids={CHAT},
                                          allow_any_private=True))
        # 但「不是本人发的」在任何宽松模式下都不认。
        self.assertFalse(help_manual.wants(message('help', chat='oc_a_private_dm', sender='ou_x'),
                                           owner_id=OWNER, self_chat_ids={CHAT},
                                           allow_any_private=True))

    def test_other_sender_does_not_hit(self):
        self.assertFalse(self.wants(message('help', sender='ou_someone_else')))

    def test_app_sender_does_not_hit(self):
        self.assertFalse(self.wants(message('help', sender_type='app')))

    def test_long_sentence_with_help_inside_does_not_hit(self):
        self.assertFalse(self.wants(message('help 我之前那个表格的事怎么样了，能现在回复吗？')))

    def test_post_message_with_help_hits(self):
        self.assertTrue(self.wants(message('help', msg_type='post')))


class ChunkTests(unittest.TestCase):
    def test_chunks_carry_order_marks_and_finish(self):
        pieces = help_manual.chunks()
        self.assertGreaterEqual(len(pieces), 1)
        for index, body in enumerate(pieces, 1):
            if len(pieces) > 1:
                self.assertIn('（第 %d/%d 条）' % (index, len(pieces)), body)
            self.assertLessEqual(len(body), help_manual.MAX_CHARS)
        self.assertTrue(pieces[-1].rstrip().endswith('（完）'))

    def test_small_budget_splits_more_and_keeps_every_chunk_in_order(self):
        pieces = help_manual.chunks(max_chars=300)
        self.assertGreater(len(pieces), 1)
        for index, body in enumerate(pieces, 1):
            self.assertLessEqual(len(body), 300)
            self.assertIn('（第 %d/%d 条）' % (index, len(pieces)), body)
        self.assertTrue(pieces[-1].rstrip().endswith('（完）'))

    def test_manual_has_no_system_words(self):
        lowered = help_manual.text().lower()
        for word in help_manual.BANNED:
            with self.subTest(word=word):
                self.assertNotIn(word.lower(), lowered)


class CliTests(unittest.TestCase):
    def test_help_command_prints_the_same_text(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(_manual([]), 0)
        self.assertEqual(out.getvalue().strip(), help_manual.text().strip())

    def test_help_json_returns_text_and_chunks(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(_manual(['--json']), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['text'], help_manual.text())
        self.assertEqual(payload['chunks'], help_manual.chunks())


class DeliveryTests(unittest.TestCase):
    def test_details_sends_manual_and_marks_ignored(self):
        folder, service = build_service(message('help'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        mid = 'om_help'
        now = time.time()
        service.store.begin_scan('live', 0, 1)
        service.store.ingest_search_page('live', [mid], None, now)
        thread = threading.Thread(target=service.details, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 8
            rows = []
            while time.time() < deadline:
                rows = service.store.connection.execute(
                    'SELECT row_id,seq,kind,inbox_id,body,state FROM outbox WHERE inbox_id=? ORDER BY seq',
                    (mid,)).fetchall()
                if len(rows) >= len(help_manual.chunks()):
                    break
                time.sleep(0.05)
        finally:
            service.stop.set()
            thread.join(5)
            assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
        pieces = help_manual.chunks()
        self.assertEqual(len(rows), len(pieces))
        self.assertEqual([row['seq'] for row in rows], list(range(1, len(pieces) + 1)))
        self.assertEqual({row['kind'] for row in rows}, {'say'})
        self.assertEqual([row['body'] for row in rows], pieces)
        self.assertEqual(rows[-1]['body'].rstrip()[-3:], '（完）')
        stored = service.store.get_message(mid)
        self.assertEqual(stored['state'], 'ignored')
        self.assertEqual(stored['error'], 'command:help')
        # 不算教材、不产生事件
        self.assertEqual(service.store.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.assertFalse((service.home / 'teach.jsonl').exists())

    def test_replayed_delivery_does_not_send_twice(self):
        folder, service = build_service(message('help'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        mid = 'om_help'
        now = time.time()
        service.store.begin_scan('live', 0, 1)
        service.store.ingest_search_page('live', [mid], None, now)
        job = service.store.claim_message('details', now, 100)
        service.store.complete_details(mid, job['attempt_id'], now, {
            'chat_id': CHAT, 'parent_id': None, 'root_id': None, 'thread_id': None,
            'create_ms': int(now * 1000), 'version': 'v1', 'raw_json': '{}',
            'text': 'help', 'sender_type': 'user', 'sender_id': OWNER},
            state='ignored', error='help_manual')
        service.send_manual(CHAT, mid, now)
        first = service.store.connection.execute(
            'SELECT COUNT(*) FROM outbox WHERE inbox_id=?', (mid,)).fetchone()[0]
        service.send_manual(CHAT, mid, now + 1)
        second = service.store.connection.execute(
            'SELECT COUNT(*) FROM outbox WHERE inbox_id=?', (mid,)).fetchone()[0]
        self.assertEqual(first, len(help_manual.chunks()))
        self.assertEqual(second, first)


if __name__ == '__main__':
    unittest.main()
