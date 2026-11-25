"""L1 任务账本：跨消息的活有一本可查的账。

判据（设计稿 §4.5）都落在这里：
1. 一个活跨 3 条消息完成 → steps 有 3 条、artifacts 有 2 个；
2. 消费者中途被 kill → 任务从 running 回到 open 再续做，重复提交不产生第二条 step；
3. 失败有状态可查（不再只是"死信 +1"）。
"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bridge.store import Store


class TaskLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def queue_message(self, mid, now, chat_id='oc_a'):
        """造一条已经排队等外部消费者的消息，返回它的 message 事件 payload。"""
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', [mid], None, now)
        job = self.store.claim_message('details', now, 10)
        payload = {'message_id': mid, 'chat_id': chat_id, 'sender_id': 'ou_a', 'text': '把三张表汇总'}
        self.assertTrue(self.store.complete_details(
            mid, job['attempt_id'], now + 1,
            {'version': 'v1', 'chat_id': chat_id, 'sender_id': 'ou_a', 'sender_type': 'user',
             'text': '把三张表汇总', 'raw_json': '{}'},
            state='external_queued',
            event={'kind': 'message', 'dedupe_key': 'message:' + mid, 'payload': payload}))
        return payload

    def claim(self, now, consumer='dsh-main', lease=600):
        self.store.register_consumer(consumer, 'dsh', now)
        events = self.store.claim_events(consumer, now, limit=5, lease_seconds=lease)
        return events[0] if events else None

    # --- 建账 ---------------------------------------------------------------
    def test_a_new_task_carries_the_message_that_started_it(self):
        task = self.store.draft_task('oc_a', '把三张表汇总成一张', 100, message_id='om_1')
        self.assertTrue(task['id'].startswith('tsk_'))
        self.assertEqual((task['chat_id'], task['state'], task['steps']), ('oc_a', 'open', []))
        self.assertEqual(self.store.task_by_message('om_1')['id'], task['id'])
        self.assertIsNone(self.store.task_by_message('om_missing'))

    def test_attaching_a_message_to_another_chats_task_is_refused(self):
        task = self.store.draft_task('oc_a', 't', 100, message_id='om_1')
        self.assertFalse(self.store.attach_task_message(task['id'], 'om_2', 101, chat_id='oc_b'))
        self.assertTrue(self.store.attach_task_message(task['id'], 'om_2', 102, chat_id='oc_a'))
        self.assertTrue(self.store.attach_task_message(task['id'], 'om_2', 103, chat_id='oc_a'))
        self.assertEqual(self.store.task_by_message('om_2')['id'], task['id'])

    def test_active_tasks_are_scoped_to_one_chat_and_skip_finished_work(self):
        self.store.draft_task('oc_a', 'A', 100)
        done = self.store.draft_task('oc_a', 'B', 101)
        self.store.set_task_state(done['id'], 'done', 102)
        self.store.draft_task('oc_b', 'C', 103)
        titles = [task['title'] for task in self.store.active_tasks('oc_a')]
        self.assertEqual(titles, ['A'])
        self.assertEqual(len(self.store.tasks()), 3)

    # --- 一步一条账 ---------------------------------------------------------
    def test_a_reply_that_reports_running_keeps_the_task_open_and_records_the_step(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', '把三张表汇总成一张', now, message_id='om_1')
        event = self.claim(now + 2)
        result = self.store.complete_external_reply(
            'dsh-main', event['id'], now + 3, '第一张读完了', 'u' * 40,
            claim_token=event['claim_token'],
            task={'state': 'running', 'note': '读完 1/3',
                  'artifacts': [{'path': '/tmp/a.xlsx', 'kind': 'file'}]})
        self.assertTrue(result['ok'])
        ledger = self.store.get_task(task['id'])
        self.assertEqual(ledger['state'], 'running')
        self.assertEqual(len(ledger['steps']), 1)
        self.assertEqual(ledger['steps'][0]['outcome'], 'sent')
        self.assertEqual(ledger['steps'][0]['note'], '读完 1/3')
        self.assertEqual([item['path'] for item in ledger['artifacts']], ['/tmp/a.xlsx'])

    def test_a_single_step_task_is_closed_by_the_only_reply_it_gets(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', '一句话的活', now, message_id='om_1')
        event = self.claim(now + 2)
        self.store.complete_external_reply('dsh-main', event['id'], now + 3, '好了', 'u' * 40,
                                           claim_token=event['claim_token'])
        ledger = self.store.get_task(task['id'])
        self.assertEqual(ledger['state'], 'done')
        self.assertEqual([step['outcome'] for step in ledger['steps']], ['sent'])

    def test_a_later_step_without_a_report_leaves_the_state_alone(self):
        # 桥不替消费者猜"做完了"：多步任务中途一步没有回填，就保持原状态等回收。
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', '把三张表汇总成一张', now, message_id='om_1')
        event = self.claim(now + 2)
        self.store.complete_external_reply('dsh-main', event['id'], now + 3, '先做第一步', 'u' * 40,
                                           claim_token=event['claim_token'],
                                           task={'state': 'running', 'note': '第一步'})
        self.queue_message('om_2', now + 10)
        self.store.attach_task_message(task['id'], 'om_2', now + 10, chat_id='oc_a')
        second = self.store.claim_events('dsh-main', now + 12, limit=5)[0]
        self.store.complete_external_reply('dsh-main', second['id'], now + 13, '第二步也好了', 'v' * 40,
                                           claim_token=second['claim_token'])
        ledger = self.store.get_task(task['id'])
        self.assertEqual(ledger['state'], 'running')
        self.assertEqual([step['outcome'] for step in ledger['steps']], ['sent', 'sent'])

    def test_replaying_the_same_reply_does_not_append_a_second_step(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', '把三张表汇总成一张', now, message_id='om_1')
        event = self.claim(now + 2)
        for _ in range(2):
            self.store.complete_external_reply('dsh-main', event['id'], now + 3, '好了', 'u' * 40,
                                               claim_token=event['claim_token'],
                                               task={'state': 'running'})
        ledger = self.store.get_task(task['id'])
        self.assertEqual(len(ledger['steps']), 1)
        self.assertEqual(ledger['state'], 'running')

    def test_blocked_needs_a_question_and_clears_it_when_work_resumes(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', '把三张表汇总成一张', now, message_id='om_1')
        event = self.claim(now + 2)
        self.store.complete_external_reply('dsh-main', event['id'], now + 3, '缺 3 月数据', 'u' * 40,
                                           claim_token=event['claim_token'],
                                           task={'state': 'blocked', 'open_question': '3 月的数据在哪个目录？'})
        self.assertEqual(self.store.get_task(task['id'])['open_question'], '3 月的数据在哪个目录？')
        self.store.set_task_state(task['id'], 'running', now + 4)
        self.assertIsNone(self.store.get_task(task['id'])['open_question'])

    def test_bad_update_fields_are_dropped_instead_of_failing_the_reply(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', 't', now, message_id='om_1')
        event = self.claim(now + 2)
        result = self.store.complete_external_reply(
            'dsh-main', event['id'], now + 3, '好了', 'u' * 40, claim_token=event['claim_token'],
            task={'state': 'nonsense', 'note': 'x' * 900, 'artifacts': ['/tmp/ok.txt', {'path': ''}, 5]})
        self.assertTrue(result['ok'])
        ledger = self.store.get_task(task['id'])
        self.assertEqual(ledger['state'], 'done')
        self.assertEqual(len(ledger['steps'][0]['note']), 500)
        self.assertEqual([item['path'] for item in ledger['artifacts']], ['/tmp/ok.txt'])

    # --- 失败与停滞 ---------------------------------------------------------
    def test_a_dead_event_records_a_failed_step_and_fails_the_task(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', '把三张表汇总成一张', now, message_id='om_1')
        event = self.claim(now + 2)
        state = self.store.fail_event('dsh-main', event['id'], now + 3, 'AgentFailed',
                                      claim_token=event['claim_token'], permanent=True)
        self.assertEqual(state, 'dead')
        ledger = self.store.get_task(task['id'])
        self.assertEqual(ledger['state'], 'failed')
        self.assertEqual(ledger['steps'][0]['outcome'], 'failed')
        self.assertIn('AgentFailed', ledger['steps'][0]['note'])

    def test_a_timed_out_event_also_fails_the_task(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', 't', now, message_id='om_1')
        self.assertEqual(self.store.expire_external(now + 99999, 60), 1)
        self.assertEqual(self.store.get_task(task['id'])['state'], 'failed')

    def test_a_finished_task_is_not_resurrected_by_a_late_event(self):
        now = time.time()
        self.queue_message('om_1', now)
        task = self.store.draft_task('oc_a', 't', now, message_id='om_1')
        self.store.set_task_state(task['id'], 'done', now + 1)
        event = self.claim(now + 2)
        self.store.fail_event('dsh-main', event['id'], now + 3, 'AgentFailed',
                              claim_token=event['claim_token'], permanent=True)
        self.assertEqual(self.store.get_task(task['id'])['state'], 'done')

    def test_recover_tasks_sends_a_stalled_task_back_to_open_then_blocks_it(self):
        now = time.time()
        task = self.store.draft_task('oc_a', 't', now - 5000)
        self.store.set_task_state(task['id'], 'running', now - 5000)
        self.assertEqual(self.store.recover_tasks(now, lease_seconds=900), 1)
        ledger = self.store.get_task(task['id'])
        self.assertEqual((ledger['state'], ledger['attempts']), ('open', 1))
        for _ in range(2):
            self.store.set_task_state(task['id'], 'running', now - 5000)
            self.store.recover_tasks(now, lease_seconds=900)
        ledger = self.store.get_task(task['id'])
        self.assertEqual(ledger['state'], 'blocked')
        self.assertIn('连续 3 轮', ledger['open_question'])

    def test_counts_make_an_active_ledger_visible_to_status(self):
        open_task = self.store.draft_task('oc_a', 'A', 100)
        blocked = self.store.draft_task('oc_a', 'B', 101)
        self.store.set_task_state(blocked['id'], 'blocked', 102, open_question='问一句')
        self.store.set_task_state(open_task['id'], 'done', 103)
        counts = self.store.task_counts()
        self.assertEqual(counts['states'], {'blocked': 1, 'done': 1})
        self.assertEqual((counts['active'], counts['blocked']), (1, 1))


class TaskMigrationTests(unittest.TestCase):
    def test_v4_database_gains_the_ledger_without_touching_existing_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.sqlite3'
            raw = sqlite3.connect(str(path))
            raw.executescript('''
                CREATE TABLE inbox (id TEXT PRIMARY KEY, state TEXT NOT NULL, received_at REAL NOT NULL,
                    next_due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    attempt_id TEXT, lease_until REAL, claim_version TEXT, chat_id TEXT,
                    parent_id TEXT, root_id TEXT, thread_id TEXT, create_ms INTEGER, version TEXT,
                    raw_json TEXT, text TEXT, sender_type TEXT, sender_id TEXT, error TEXT);
                INSERT INTO inbox(id,state,received_at) VALUES('om_old','completed',1);
                PRAGMA user_version=4;
            ''')
            raw.commit()
            raw.close()
            store = Store(path)
            try:
                self.assertEqual(
                    store.connection.execute('PRAGMA user_version').fetchone()[0], Store.SCHEMA_VERSION)
                self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)
                self.assertEqual(store.tasks(), [])
                self.assertEqual(store.draft_task('oc_a', '新活', 10)['state'], 'open')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
