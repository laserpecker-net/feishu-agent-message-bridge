"""L2-b 归因与复盘：turns.jsonl 的写入、幂等、统计与机械归纳。

判据落在这里（可复跑）：
- 每轮结局都有记录（sent / failed / timeout），同一事件重试只留最新一条；
- 统计能看出失败类别与教材使用量；
- 复盘只在同一类失败出现 ≥2 次时给候选教训，文本来自模板、证据能指回具体 turn；
- 候选教训要本人回「记」才落盘（在 tests/test_service.py 的复盘用例里）。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bridge import teach, turns


class TurnsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, event_id='e1', outcome='sent', **kwargs):
        return turns.record(self.home, message_id=kwargs.pop('message_id', 'om_1'),
                            event_id=event_id, outcome=outcome, **kwargs)


class RecordTests(TurnsCase):
    def test_a_turn_carries_the_facts_that_were_known_at_the_time(self):
        entry = self.record(used_ids=['t-0001', 't-0003'], task_id='tsk_1', chat_id='oc_1',
                            consumer='dsh-main')
        self.assertEqual(entry['outcome'], 'sent')
        self.assertEqual(entry['used_ids'], ['t-0001', 't-0003'])
        self.assertEqual(entry['task_id'], 'tsk_1')
        self.assertEqual(turns.turns(self.home)['turns'][0]['event_id'], 'e1')

    def test_the_log_file_is_private(self):
        self.record()
        mode = os.stat(turns.log_path(self.home)).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_the_same_event_only_keeps_the_latest_outcome(self):
        # 重试成功之后，归因里不该还留着上一次的失败记录。
        self.record(event_id='e1', outcome='failed', error='AgentFailed')
        self.record(event_id='e1', outcome='sent')
        self.record(event_id='e2', outcome='sent', message_id='om_2')
        items = turns.turns(self.home)['turns']
        self.assertEqual([item['event_id'] for item in items], ['e2', 'e1'])
        self.assertEqual(items[1]['outcome'], 'sent')

    def test_an_unknown_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            self.record(outcome='mostly-fine')

    def test_a_corrupt_line_is_reported_not_swallowed(self):
        self.record()
        with turns.log_path(self.home).open('a') as handle:
            handle.write('{not json\n')
        result = turns.turns(self.home)
        self.assertEqual(result['count'], 1)
        self.assertIn('1 行读不出来', result['error'])

    def test_the_log_is_capped_and_keeps_the_newest(self):
        original = turns.MAX_TURNS
        turns.MAX_TURNS = 3
        try:
            for index in range(5):
                self.record(event_id='e%d' % index)
        finally:
            turns.MAX_TURNS = original
        self.assertEqual(len(turns.turns(self.home, limit=10)['turns']), 3)


class SummaryTests(TurnsCase):
    def test_summary_counts_outcomes_errors_and_teaching_usage(self):
        self.record(event_id='e1', used_ids=['t-0001', 't-0002'])
        self.record(event_id='e2', used_ids=[])
        self.record(event_id='e3', outcome='failed', error='AgentFailed: adapter crashed')
        self.record(event_id='e4', outcome='failed', error='AgentFailed: adapter crashed')
        self.record(event_id='e5', outcome='timeout', error='External reply timeout')
        stats = turns.summary(self.home)
        self.assertEqual(stats['by_outcome'], {'sent': 2, 'skipped': 0, 'failed': 2, 'timeout': 1})
        self.assertEqual(stats['errors'][0], ('AgentFailed', 2))
        self.assertEqual(stats['teachings']['turns_with_teachings'], 1)
        self.assertEqual(stats['teachings']['average_used'], 2.0)
        self.assertEqual(stats['teachings']['sent_without_attribution'], 1)

    def test_errors_are_grouped_by_class_not_by_message_id(self):
        self.record(event_id='e1', outcome='failed', error='AgentFailed')
        self.record(event_id='e2', outcome='failed', error='AgentFailed')
        self.assertEqual(len(turns.summary(self.home)['errors']), 1)


class ReflectionTests(TurnsCase):
    def test_a_single_failure_is_not_turned_into_a_lesson(self):
        self.record(outcome='failed', error='AgentFailed')
        report = turns.reflection(self.home)
        self.assertIsNone(report['candidate'])
        self.assertIn('不编教训', report['report'])

    def test_a_repeated_failure_produces_one_candidate_with_evidence(self):
        self.record(event_id='e1', outcome='failed', error='AgentFailed')
        self.record(event_id='e2', outcome='failed', error='AgentFailed')
        self.record(event_id='e3', outcome='failed', error='InsufficientTime')
        report = turns.reflection(self.home)
        candidate = report['candidate']
        self.assertEqual(candidate['kind'], 'lesson')
        self.assertEqual(candidate['error_kind'], 'AgentFailed')
        self.assertEqual(candidate['evidence'], ['e1', 'e2'])
        self.assertTrue(candidate['text'].startswith('教训：'))
        self.assertIn('记', report['report'])

    def test_nothing_to_reflect_on_says_so_instead_of_inventing(self):
        report = turns.reflection(self.home)
        self.assertIsNone(report['candidate'])
        self.assertIn('还没有记录', report['report'])

    def test_a_clean_run_gets_no_lesson(self):
        for index in range(5):
            self.record(event_id='e%d' % index)
        report = turns.reflection(self.home)
        self.assertIsNone(report['candidate'])
        self.assertIn('答完 5 次', report['report'])


class LessonKindTests(TurnsCase):
    def test_a_lesson_is_a_searchable_kind_with_its_own_expose_default(self):
        self.assertIn('lesson', teach.KINDS)
        self.assertIn('lesson', teach.SEARCH_KINDS)
        self.assertEqual(teach.DEFAULT_EXPOSE['lesson'], 'say')
        self.assertEqual(teach.KIND_LABEL['lesson'], '教训')
        record = teach.new_entry('lesson', '拆成小步，每步回执', 'om_x', time.time() * 1000)
        self.assertEqual(record['kind'], 'lesson')

    def test_a_candidate_lesson_is_offered_but_not_written_by_reflection(self):
        # 复盘是只读的归因报告：候选教训要本人同意才落盘，而且"同意"由模型判（没有命令词）。
        # 这里钉的是报告本身：同一类失败重复够多次才给候选，且一条都没自动写进教材。
        for index in range(3):
            turns.record(self.home, message_id='om_%d' % index, event_id='e%d' % index,
                         outcome='failed', error='AgentFailed')
        report = turns.reflection(self.home)
        self.assertIsNotNone(report['candidate'])
        self.assertTrue(report['candidate']['text'].startswith('教训：'))
        self.assertIn('教训：', report['report'])
        self.assertEqual(teach.state(self.home)['notes'], [])


if __name__ == '__main__':
    unittest.main()


class StoreWiringTests(unittest.TestCase):
    """归因由 store 在事务提交后统一写（一个事实一个答案，不在 CLI/消费者各写一遍）。"""

    def setUp(self):
        from bridge.store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.now = 1000.0
        self.store.register_consumer('dsh-main', 'dsh', self.now)
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', ['om_1'], None, self.now)
        job = self.store.claim_message('details', self.now, 10)
        self.payload = {'message_id': 'om_1', 'chat_id': 'oc_a', 'sender_id': 'ou_a', 'text': 'x'}
        self.store.complete_details(
            'om_1', job['attempt_id'], self.now + 1,
            {'version': 'v1', 'chat_id': 'oc_a', 'sender_id': 'ou_a', 'sender_type': 'user',
             'text': 'x', 'raw_json': '{}'}, state='external_queued',
            event={'kind': 'message', 'dedupe_key': 'message:om_1', 'payload': self.payload})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def claim(self):
        claimed = self.store.claim_events('dsh-main', self.now + 2, limit=1, lease_seconds=600)
        self.assertEqual(len(claimed), 1)
        return claimed[0]

    def test_a_sent_reply_is_recorded_with_the_teachings_the_model_saw(self):
        event = self.claim()
        self.store.set_event_used_ids(event['id'], ['t-0001', 't-0002'])
        result = self.store.complete_external_reply(
            'dsh-main', event['id'], self.now + 3, '答完这句', 'uuid-1',
            claim_token=event['claim_token'])
        self.assertTrue(result['ok'])
        items = turns.turns(self.home)['turns']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['outcome'], items[0]['used_ids'], items[0]['message_id']),
                         ('sent', ['t-0001', 't-0002'], 'om_1'))
        self.assertEqual(items[0]['consumer'], 'dsh-main')  # 谁做的也是归因的一部分

    def test_a_failed_event_is_recorded_and_a_later_success_replaces_it(self):
        event = self.claim()
        self.store.fail_event('dsh-main', event['id'], self.now + 3, 'AgentFailed',
                             claim_token=event['claim_token'])
        self.assertEqual(turns.turns(self.home)['turns'][0]['outcome'], 'failed')
        again = self.store.claim_events('dsh-main', self.now + 100, limit=1, lease_seconds=600)[0]
        self.store.complete_external_reply('dsh-main', again['id'], self.now + 101, '答完这句',
                                           'uuid-1', claim_token=again['claim_token'])
        items = turns.turns(self.home)['turns']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['outcome'], 'sent')

    def test_a_timed_out_event_is_recorded_as_timeout(self):
        self.claim()
        self.assertEqual(self.store.expire_external(self.now + 5000, 600), 1)
        self.assertEqual(turns.turns(self.home)['turns'][0]['outcome'], 'timeout')

    def test_a_rolled_back_transaction_leaves_no_turn_behind(self):
        # 事务没成的时候，"这一轮结局"其实不成立（事件可能还是 leased），不能记进归因。
        event = self.claim()
        with self.assertRaises(RuntimeError):
            with self.store._transaction():
                self.store._queue_turn(event, 'sent')
                raise RuntimeError('disk full')
        self.assertEqual(turns.turns(self.home)['count'], 0)
