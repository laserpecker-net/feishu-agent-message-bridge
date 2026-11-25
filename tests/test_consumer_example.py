"""Focused protocol checks for the copyable consumer reference."""
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import consumer_runtime as example


EXAMPLE = Path(__file__).resolve().parents[1] / 'consumer-example.py'


class ExampleConsumerTests(unittest.TestCase):
    def test_unimplemented_adapter_exits_before_touching_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'untouched'
            result = subprocess.run([sys.executable, str(EXAMPLE), '--consumer', 'example-agent',
                                     '--home', str(home), '--check'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Implement build_agent()', result.stderr)
            self.assertFalse(home.exists())

    def test_unknown_submit_outcome_reuses_saved_answer_and_token(self):
        now = time.time()
        event = {'id': 17, 'kind': 'message', 'claim_token': 'a' * 32,
                 'created_at': now, 'payload': {'text': '43×23', 'context': []}}
        row = dict(event, consumer='example-agent', state='leased', lease_until=now + 600)
        generated = []
        submitted = []

        def generate(payload, event, stop_event):
            generated.append(payload['text'])
            return '989'

        def call(argv, body=None):
            if argv[0] == 'list':
                return {'events': [row]}
            if argv[0] == 'status':
                return {'external': {'reply_timeout_seconds': 600}}
            if argv[0] == 'reply':
                self.assertEqual(argv[-2:], ['--claim-token', 'a' * 32])
                self.assertNotIn('989', argv)
                submitted.append(body)
                row['state'] = 'done'
                if len(submitted) == 1:
                    raise RuntimeError('response lost after commit')
                return {'ok': True, 'duplicate': True}
            self.fail('unexpected command: ' + repr(argv))

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                example.process(call, 'example-agent', state, event, generate, threading.Event())
            example.process(call, 'example-agent', state, event, generate, threading.Event())
            self.assertEqual(generated, ['43×23'])
            self.assertEqual(submitted, ['989', '989'])
            record = example.json.loads(example.attempt_path(state, event).read_text())
            self.assertTrue(record['receipt']['duplicate'])

    def test_status_without_the_deleted_external_switch_still_uses_the_full_budget(self):
        # 真机事故：`status.external` 只剩 reply_timeout_seconds 之后，消费者每条消息都算出 0 秒，
        # 立刻以 InsufficientTime 放弃（inbox → needs_attention）。判据：不给 `enabled` 也要跑完。
        now = time.time()
        event = {'id': 91, 'kind': 'message', 'claim_token': 'c' * 32,
                 'created_at': now, 'payload': {'text': '1+1', 'context': []}}
        row = dict(event, consumer='example-agent', state='leased', lease_until=now + 600)
        failed = []

        def generate(payload, event, stop_event):
            return '2'

        def call(argv, body=None):
            if argv[0] == 'list':
                return {'events': [row]}
            if argv[0] == 'status':
                return {'external': {'reply_timeout_seconds': 600}}
            if argv[0] == 'fail':
                failed.append(argv)
                return {'ok': True}
            if argv[0] == 'reply':
                row['state'] = 'done'
                return {'ok': True}
            self.fail('unexpected command: ' + repr(argv))

        with tempfile.TemporaryDirectory() as temp:
            example.process(call, 'example-agent', Path(temp), event, generate, threading.Event())
        self.assertEqual(failed, [])

    def test_restart_recovers_claim_committed_before_journal_write(self):
        class OneIteration(BaseException):
            pass

        now = time.time()
        row = {'id': 23, 'kind': 'message', 'claim_token': 'b' * 32,
               'created_at': now, 'payload': {'text': '47×13', 'context': []},
               'consumer': 'example-agent', 'state': 'leased', 'lease_until': now + 600}
        calls = []

        def fake_command(_binary, _home, argv, body=None):
            calls.append((argv[0], body))
            if argv[0] == 'register':
                return {'ok': True}
            if argv[0] == 'list':
                return {'events': [row]}
            if argv[0] == 'status':
                return {'external': {'reply_timeout_seconds': 600}}
            if argv[0] == 'reply':
                self.assertEqual(body, '611')
                return {'ok': True}
            self.fail('A new claim must not precede recovery')

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with tempfile.TemporaryDirectory() as temp, \
                 mock.patch.object(sys, 'argv', [str(EXAMPLE), '--consumer', 'example-agent',
                                                 '--home', temp]), \
                 mock.patch.object(example, 'command', side_effect=fake_command), \
                 mock.patch.object(example.time, 'sleep', side_effect=OneIteration):
                # 真实 home 一定有 config.json（install.py 写的）；没有它消费者现在会立刻
                # SystemExit，而不是在门禁里空转。
                Path(temp, 'config.json').write_text(
                    example.json.dumps({'sending_enabled': True, 'target_open_id': 'ou_self'}))
                with self.assertRaises(OneIteration):
                    example.main(lambda _home: lambda _p, _e, _s: '611')
                journal = Path(temp) / 'consumer-state/example-agent/23' / ('b' * 32) / 'attempt.json'
                self.assertTrue(example.json.loads(journal.read_text())['receipt']['ok'])
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        self.assertEqual([item[0] for item in calls],
                         ['register', 'list', 'list', 'status', 'list', 'status', 'reply'])

    def test_configured_external_deadline_is_not_assumed_to_be_600(self):
        now = time.time()
        event = {'id': 29, 'kind': 'message', 'claim_token': 'c' * 32,
                 'created_at': now - 400, 'payload': {'text': '请回答'}}
        row = dict(event, consumer='example-agent', state='leased', lease_until=now + 600)
        submitted = []

        def call(argv, body=None):
            if argv[0] == 'list':
                return {'events': [row]}
            if argv[0] == 'status':
                return {'external': {'reply_timeout_seconds': 900}}
            if argv[0] == 'reply':
                submitted.append(body)
                return {'ok': True}
            self.fail('unexpected command: ' + repr(argv))

        with tempfile.TemporaryDirectory() as temp:
            example.process(call, 'example-agent', Path(temp), event,
                            lambda _payload, _event, _stop: '可以', threading.Event())
        self.assertEqual(submitted, ['可以'])

    def test_stop_before_generation_keeps_live_claim_recoverable(self):
        now = time.time()
        event = {'id': 31, 'kind': 'message', 'claim_token': 'd' * 32,
                 'created_at': now, 'payload': {'text': '恢复后处理'}}
        row = dict(event, consumer='example-agent', state='leased', lease_until=now + 600)
        submitted = []

        def call(argv, body=None):
            if argv[0] == 'list':
                return {'events': [row]}
            if argv[0] == 'status':
                return {'external': {'reply_timeout_seconds': 600}}
            if argv[0] == 'reply':
                submitted.append(body)
                return {'ok': True}
            self.fail('unexpected command: ' + repr(argv))

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            stopped = threading.Event()
            stopped.set()
            example.process(call, 'example-agent', state, event,
                            lambda _p, _e, _s: self.fail('must not generate while stopping'), stopped)
            record = example.json.loads(example.attempt_path(state, event).read_text())
            self.assertNotIn('abandoned', record)
            example.process(call, 'example-agent', state, event,
                            lambda _p, _e, _s: '已恢复', threading.Event())
        self.assertEqual(submitted, ['已恢复'])


if __name__ == '__main__':
    unittest.main()
