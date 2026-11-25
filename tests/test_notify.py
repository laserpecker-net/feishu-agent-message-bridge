import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest

from bridge import notify


class DecideTests(unittest.TestCase):
    def test_first_notification_is_sent(self):
        self.assertEqual(notify.decide({}, 'k1', now=1000)[0], 'send')

    def test_same_key_inside_window_is_a_duplicate(self):
        state = notify.record({}, 'k1', now=1000)
        self.assertEqual(notify.decide(state, 'k1', now=1000 + 60)[0], 'duplicate')

    def test_same_key_after_window_is_sent_again(self):
        state = notify.record({}, 'k1', now=1000)
        later = 1000 + notify.DEDUPE_WINDOW_SECONDS + 1
        self.assertEqual(notify.decide(state, 'k1', now=later)[0], 'send')

    def test_hourly_cap_keeps_a_burst_quiet(self):
        state = {}
        for index in range(notify.MAX_PER_HOUR):
            self.assertEqual(notify.decide(state, 'k%d' % index, now=1000)[0], 'send')
            state = notify.record(state, 'k%d' % index, now=1000)
        decision, reason = notify.decide(state, 'fresh', now=1000)
        self.assertEqual(decision, 'capped')
        self.assertIn('达到上限', reason)

    def test_cap_slides_with_time(self):
        state = {}
        for index in range(notify.MAX_PER_HOUR):
            state = notify.record(state, 'k%d' % index, now=1000)
        self.assertEqual(notify.decide(state, 'fresh', now=1000 + 3601)[0], 'send')

    def test_corrupt_state_is_treated_as_empty(self):
        with __import__('tempfile').TemporaryDirectory() as directory:
            home = Path(directory)
            notify.state_path(home).write_text('{not json')
            self.assertEqual(notify.read_state(home), {})


class RecordTests(unittest.TestCase):
    def test_record_keeps_the_dm_chat_id(self):
        state = notify.record({}, 'k1', now=1000, chat_id='oc_dm')
        self.assertEqual(state['owner_dm_chat_id'], 'oc_dm')

    def test_record_drops_expired_keys(self):
        state = notify.record({}, 'old', now=1000)
        later = 1000 + notify.DEDUPE_WINDOW_SECONDS + 1
        state = notify.record(state, 'new', now=later)
        self.assertEqual(list(state['sent']), ['new'])

    def test_body_key_ignores_whitespace_only_differences(self):
        self.assertEqual(notify.body_key('需要你确认\n\nA 还是 B'), notify.body_key('需要你确认 A 还是 B'))
        self.assertNotEqual(notify.body_key('A'), notify.body_key('B'))


if __name__ == '__main__':
    unittest.main()


class NotifyQuestionRegistrationTests(unittest.TestCase):
    """notify 发出求助后必须登记待答，否则本人引用回复时没有任何东西能对上号。"""

    def test_question_flag_registers_the_dm_message_id(self):
        import json
        import tempfile
        from unittest.mock import patch
        from bridge import cli, teach

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps(
                {'target_open_id': 'ou_owner', 'sending_enabled': True}))
            sent = {'message_id': 'om_dm9', 'chat_id': 'oc_dm'}
            with patch.object(cli, '_read_reply_body', return_value='【需要你出手】要不要按 A 做？\n\n· 有人问到：x'), \
                 patch('bridge.transport.Transport.send_dm', return_value=sent), \
                 patch('builtins.print'):
                code = cli._notify(home, ['--dedupe-key', 'event:7', '--question-event', '7',
                                          '--needs', 'decision'])
            self.assertEqual(code, 0)
            pending = teach.pending_questions(home)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]['dm_message_id'], 'om_dm9')
            self.assertEqual(pending[0]['chat_id'], 'oc_dm')
            self.assertEqual(pending[0]['needs'], 'decision')
            self.assertIn('要不要按 A 做', pending[0]['question'])

    def test_without_the_flag_nothing_is_registered(self):
        import json
        import tempfile
        from unittest.mock import patch
        from bridge import cli, teach

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.json').write_text(json.dumps(
                {'target_open_id': 'ou_owner', 'sending_enabled': True}))
            sent = {'message_id': 'om_dm9', 'chat_id': 'oc_dm'}
            with patch.object(cli, '_read_reply_body', return_value='普通提醒'), \
                 patch('bridge.transport.Transport.send_dm', return_value=sent), \
                 patch('builtins.print'):
                code = cli._notify(home, ['--dedupe-key', 'event:7'])
            self.assertEqual(code, 0)
            self.assertEqual(teach.pending_questions(home), [])
