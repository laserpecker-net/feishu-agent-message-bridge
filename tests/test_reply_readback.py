"""回读校验：含 @ 的回复为什么曾经 100% 误判，判据改成结构等价后怎么判。

判据能直接重跑：``python3 -m pytest tests/test_reply_readback.py -q``。
生产事实见 docs/HISTORY.md（2026-11-18）：238 条不含 @ 的回复全部通过，
14 条含 @ 的一次都没有通过，全部被标成 needs_attention——而飞书上已经发出去了。
"""

import json
import tempfile
import unittest
from pathlib import Path

from bridge import reverify
from bridge.store import Store
from bridge.transport import canonical_text, mention_ids, verify_reply, verify_reply_reason


def message(text, mentions=None, chat_id='oc_1', parent='om_1', sender='ou_me'):
    reply = {'chat_id': chat_id, 'parent_id': parent, 'sender': {'id': sender},
             'body': {'content': json.dumps({'text': text}, ensure_ascii=False)}}
    if mentions is not None:
        reply['mentions'] = [{'id': item} if isinstance(item, str) else item for item in mentions]
    return reply


ORIGINAL = {'message_id': 'om_1', 'chat_id': 'oc_1'}

WITH_AT = '<at user_id="ou_1"></at> 收到，<at user_id="ou_2"></at> 你看下'


class CanonicalTextCases(unittest.TestCase):
    def test_at_tag_and_rendered_name_are_the_same_thing(self):
        self.assertEqual(canonical_text(WITH_AT), canonical_text('@张三 收到，@李四 你看下'))

    def test_plain_text_is_untouched(self):
        self.assertEqual(canonical_text('answer'), 'answer')
        self.assertNotEqual(canonical_text('answe r'), canonical_text('answer'))

    def test_mention_ids_reads_both_shapes(self):
        self.assertEqual(mention_ids(WITH_AT), {'ou_1', 'ou_2'})
        self.assertEqual(mention_ids({'mentions': [{'id': {'open_id': 'ou_1'}}, {'id': 'ou_2'}]}),
                         {'ou_1', 'ou_2'})


class VerifyReplyCases(unittest.TestCase):
    def test_plain_reply_still_passes(self):
        self.assertTrue(verify_reply(message('answer'), ORIGINAL, 'answer', 'ou_me'))

    def test_one_and_many_mentions_pass(self):
        one = '<at user_id="ou_1"></at> 收到'
        self.assertTrue(verify_reply(message('@张三 收到', mentions=['ou_1']), ORIGINAL, one, 'ou_me'))
        self.assertTrue(verify_reply(message('@张三 收到，@李四 你看下', mentions=['ou_1', 'ou_2']),
                                     ORIGINAL, WITH_AT, 'ou_me'))

    def test_changed_character_is_still_a_failure(self):
        reason = verify_reply_reason(message('@张三 收到，@李四 你看下'), ORIGINAL, WITH_AT + '！', 'ou_me')
        self.assertEqual(reason, 'Reply readback mismatch; no resend')

    def test_wrong_mention_is_a_mentions_mismatch(self):
        reason = verify_reply_reason(message('@张三 收到，@李四 你看下', mentions=['ou_1', 'ou_9']),
                                     ORIGINAL, WITH_AT, 'ou_me')
        self.assertEqual(reason, 'Reply mentions mismatch; no resend')

    def test_missing_mentions_field_is_a_mentions_mismatch(self):
        reason = verify_reply_reason(message('@张三 收到，@李四 你看下'), ORIGINAL, WITH_AT, 'ou_me')
        self.assertEqual(reason, 'Reply mentions mismatch; no resend')

    def test_wrong_sender_and_deleted_are_reported_separately(self):
        self.assertEqual(verify_reply_reason(message('answer'), ORIGINAL, 'answer', 'ou_other'),
                         'Reply sender mismatch; no resend')
        reply = message('answer')
        reply['deleted'] = True
        self.assertEqual(verify_reply_reason(reply, ORIGINAL, 'answer', 'ou_me'),
                         'Reply deleted or wrong chat; no resend')


if __name__ == '__main__':
    unittest.main()
