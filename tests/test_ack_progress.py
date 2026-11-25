"""答得慢要先应一声、再报一次进度（2026-11-19 真机：4 分钟群里一片安静）。

这两条提醒的判据全在"别吵"上：快答不发、只发一次、回复进了队列就不再补话。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import consumer_runtime
from bridge import report as report_module
from bridge import workbench


TOKEN = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'


class Recorder:
    """假 `call`：记下每一次 CLI 调用（argv + body），按命令返回可用的结果。"""

    def __init__(self, reply_ok=True):
        self.calls = []
        self.reply_ok = reply_ok

    def __call__(self, argv, body=None):
        self.calls.append((list(argv), body))
        head = argv[0]
        if head == 'say':
            return {'ok': True, 'inbox_id': 'om_x', 'row_id': len(self.calls)}
        if head == 'reply':
            return {'ok': self.reply_ok}
        if head == 'fail':
            return {'ok': True}
        return {'ok': True}

    def bodies(self, head):
        return [body for argv, body in self.calls if argv[0] == head]


def event(payload=None, event_id=1, token=TOKEN):
    return {'id': event_id, 'claim_token': token, 'kind': 'message',
            'created_at': time.time(),
            'payload': payload if payload is not None else
            {'message_id': 'om_x', 'chat_id': 'oc_x', 'text': '你好', 'chat_type': 'group'}}


class FastAnswerTests(unittest.TestCase):
    def test_fast_answer_never_pings(self):
        recorder = Recorder()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event(),
                                             ack_seconds=0.30).start()
        time.sleep(0.05)
        self.assertTrue(notice.finished(), '提醒线程必须在答完后退出')
        self.assertEqual(recorder.calls, [], '15 秒内答完不该多发一个字')

    def test_answer_before_the_deadline_sends_nothing(self):
        recorder = Recorder()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event(),
                                             ack_seconds=10.0).start()
        time.sleep(0.1)
        notice.finished()
        self.assertEqual(recorder.bodies('say'), [])


class AckTests(unittest.TestCase):
    def test_one_ack_when_slow(self):
        recorder = Recorder()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event(),
                                             ack_seconds=0.05, progress_seconds=30.0).start()
        self.assertTrue(_wait_for(lambda: recorder.calls, 2.0), '慢答复必须应一声')
        notice.finished()
        argv, body = recorder.calls[0]
        self.assertEqual(argv[0], 'say')
        self.assertIn('--uuid', argv)
        self.assertEqual(argv[argv.index('--uuid') + 1], 'ack:om_x')
        self.assertEqual(argv[argv.index('--event') + 1], '1')
        self.assertEqual(argv[argv.index('--claim-token') + 1], TOKEN)
        self.assertEqual(body, consumer_runtime.SLOW_ACK_TEXT)
        self.assertEqual(len(recorder.calls), 1, '只说一声，不重复')

    def test_no_ack_when_the_reply_is_already_queued(self):
        recorder = Recorder()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event(), ack_seconds=0.2).start()
        notice.finished()          # 回复已进 outbox，等价于"答完了"
        time.sleep(0.35)
        self.assertEqual(recorder.calls, [])


class ProgressTests(unittest.TestCase):
    def test_ack_then_exactly_one_progress_with_the_last_step(self):
        recorder = Recorder()
        workbench.STEP_BOARD.clear()
        workbench.note_step('调用工具 bash')
        try:
            notice = consumer_runtime.SlowNotice(recorder, 'c', event(),
                                                 ack_seconds=0.05, progress_seconds=0.15).start()
            self.assertTrue(_wait_for(lambda: len(recorder.calls) >= 2, 3.0), '180 秒后要报一次进度')
            time.sleep(0.2)
            notice.finished()
        finally:
            workbench.STEP_BOARD.clear()
        said = recorder.bodies('say')
        self.assertEqual(len(said), 2, 'ack + progress，各一次')
        self.assertEqual(recorder.calls[1][0][recorder.calls[1][0].index('--uuid') + 1],
                         'progress:om_x')
        self.assertIn('卡在', said[1])
        self.assertIn('调用工具 bash', said[1])

    def test_progress_has_a_human_fallback_step(self):
        recorder = Recorder()
        workbench.STEP_BOARD.clear()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event(),
                                             ack_seconds=0.02, progress_seconds=0.05).start()
        self.assertTrue(_wait_for(lambda: len(recorder.calls) >= 2, 3.0))
        notice.finished()
        self.assertIn(consumer_runtime.SLOW_PROGRESS_FALLBACK, recorder.bodies('say')[1])


class IdempotenceTests(unittest.TestCase):
    def test_uuid_is_stable_so_a_replay_does_not_speak_twice(self):
        records = []
        for _ in range(2):
            recorder = Recorder()
            notice = consumer_runtime.SlowNotice(recorder, 'c', event(),
                                                 ack_seconds=0.02, progress_seconds=30.0).start()
            self.assertTrue(_wait_for(lambda: recorder.calls, 2.0))
            notice.finished()
            argv = recorder.calls[0][0]
            records.append(argv[argv.index('--uuid') + 1])
        self.assertEqual(records[0], records[1], '重放必须用同一个键，桥才会去重')
        self.assertEqual(records[0], 'ack:om_x')

    def test_no_message_without_a_source_message(self):
        recorder = Recorder()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event({'chat_id': 'oc_x'}),
                                             ack_seconds=0.02).start()
        time.sleep(0.1)
        notice.finished()
        self.assertEqual(recorder.calls, [])


class ThreadDisciplineTests(unittest.TestCase):
    def test_finished_joins_the_thread(self):
        recorder = Recorder()
        notice = consumer_runtime.SlowNotice(recorder, 'c', event(), ack_seconds=30.0).start()
        self.assertTrue(notice.finished())
        self.assertFalse([t for t in threading.enumerate() if t.name == 'slow-notice'])

    def test_process_finishes_the_notice_even_when_the_adapter_explodes(self):
        # 适配器炸了：运行时自己收尾（fail + 私聊通知），不会把异常抛给主循环；
        # 但提醒线程必须被收掉，否则它会在线程泄漏判据里变红。
        recorder = Recorder()
        both = _event_call(recorder)

        def explode(*_args, **_kwargs):
            raise RuntimeError('adapter down')

        with tempfile.TemporaryDirectory() as tmp:
            consumer_runtime.process(both, 'c', Path(tmp), event(), explode, threading.Event())
        self.assertIn('fail', [argv[0] for argv, _ in recorder.calls])
        self.assertFalse([t for t in threading.enumerate() if t.name == 'slow-notice'],
                         '失败路径也不许留下线程')
        self.assertEqual(recorder.bodies('say'), [], '失败得这么快，不该先应一声')

    def test_process_never_pings_when_the_reply_lands_immediately(self):
        recorder = Recorder()
        both = _event_call(recorder)
        with tempfile.TemporaryDirectory() as tmp:
            consumer_runtime.process(both, 'c', Path(tmp), event(),
                                     lambda *_: json.dumps({'answer': '好'}), threading.Event())
        self.assertEqual(recorder.bodies('say'), [], '快答不发回执')
        self.assertEqual(len(recorder.bodies('reply')), 1)


class WordingTests(unittest.TestCase):
    def test_both_messages_are_plain_language(self):
        # 与卡片/回写共用同一份禁用词表（bridge/report.speakable），不另写一份判据。
        report_module.speakable(consumer_runtime.SLOW_ACK_TEXT)
        report_module.speakable(consumer_runtime.SLOW_PROGRESS_TEXT % '调用工具 bash')
        report_module.speakable(consumer_runtime.SLOW_PROGRESS_FALLBACK)


def _event_call(recorder, reply_timeout=3600):
    """把 `list` / `status` 这两条协议查询也答上，其余交给 Recorder。

    注意 `reply_timeout` 要给得足够大：运行时要求"剩余时间 > 240 秒 + 3 条命令的预算"
    才肯开跑（否则判 InsufficientTime），而事件窗口本身只有 240 秒时永远算不过。
    """
    event_row = {'id': 1, 'consumer': 'c', 'claim_token': TOKEN,
                 'state': 'leased', 'lease_until': time.time() + 600}

    def call(argv, body=None):
        if argv[0] == 'list':
            return {'events': [event_row]}
        if argv[0] == 'status':
            return {'external': {'reply_timeout_seconds': reply_timeout}}
        return recorder(argv, body)

    return call


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


if __name__ == '__main__':
    unittest.main()
