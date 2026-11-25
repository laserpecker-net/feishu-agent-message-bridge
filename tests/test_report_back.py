"""PR-4 判据：结果回飞书 + 六种失败的人话 + 被拦住之后用「执行」接着干。

这一段有三件事最容易做错，所以每条都有独立用例：

1. **顺序**：入队 → 落账 → ack。先 ack 再发，崩溃就把结论永久留在账本里；
2. **幂等**：同一次结论重放不刷屏；重派之后是新结论，不能被上一次的键吞掉；
3. **接得上**：失败/被拦住不是终点——交接单要能发出去，`执行` 要能重新派活。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge import report
from bridge.service import Service
from bridge.store import Store
from bridge.workbench import Handoff, WorkResult

OWNER = 'ou_owner'
DM_CHAT = 'oc_owner_dm'


def handoff_for(task_id='tsk', failure='超时了', last_step='调用工具'):
    return Handoff(task_id=task_id, requirement='把表汇总', last_step=last_step,
                   progress_lines=['读需求', '拉数据'], artifacts=['out/表.csv'],
                   workspace='/wt', space='/wt/space', session_id='sess-1',
                   resume_command='dsh headless --session-id sess-1', failure=failure)


class FakeWorkbench:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def prepare(self):
        return None

    def run(self, prompt, *, task_id, requirement, session_id=None, stop=None):
        self.calls.append(task_id)
        return self.result


class ReportTextTests(unittest.TestCase):
    """文案层：说人话、一处定义、长度有上限、分条规则与说明书同一套。"""

    def test_done_summary_reads_like_a_person(self):
        body = report.group_done(answer='三份 csv 汇总好了：128 行、7 个字段。',
                                 artifacts=[{'path': 'summary.csv', 'bytes': 2048}],
                                 duration_seconds=95, space='/wt/tsk_1/space')
        self.assertIn('这件活干完了', body)
        self.assertIn('128 行', body)
        self.assertIn('summary.csv', body)
        self.assertIn('2.0 KB', body)
        self.assertNotIn('提交', body, '没有 git 的机器上没有"提交"这回事')
        self.assertIn('2 分钟', body)
        report.speakable(body)  # 不含系统词

    def test_every_kind_has_a_plain_line(self):
        for kind in ('timeout', 'no_final', 'lock_conflict', 'credential_missing',
                     'cli_unavailable', 'worktree', 'error'):
            with self.subTest(kind=kind):
                line = report.line_for(kind)
                self.assertTrue(line)
                report.speakable(line)

    def test_failure_points_at_dm_and_handoff(self):
        with_dm = report.group_failure(kind='no_final', dm_sent=True)
        self.assertIn('机器好像睡过去了', with_dm)
        self.assertIn('私聊', with_dm)
        without_dm = report.group_failure(kind='timeout', last_step='调用工具',
                                          dm_sent=False, handoff_path='work/tsk/handoff.md')
        self.assertIn('卡在「调用工具」', without_dm)
        self.assertIn('work/tsk/handoff.md', without_dm)

    def test_blocked_asks_for_execute_not_a_new_word(self):
        body = report.group_blocked(kind='lock_conflict', dm_sent=True)
        self.assertIn('回我「执行」', body)
        self.assertNotIn('继续', body)  # 不新造命令词

    def test_summary_is_trimmed_to_the_limit(self):
        body = report.group_done(answer='很长的结论。' * 400, duration_seconds=1)
        self.assertLessEqual(len(body), report.GROUP_LIMIT)
        self.assertTrue(body.endswith('…'))

    def test_dm_handoff_uses_the_same_chunking_rules(self):
        pieces = report.dm_messages(handoff_for(failure='超时了。' * 400))
        self.assertGreater(len(pieces), 1)
        self.assertIn('（第 1/%d 条）' % len(pieces), pieces[0])
        self.assertIn('（完）', pieces[-1])
        for piece in pieces:
            report.speakable(piece)

    def test_uuids_carry_the_attempt(self):
        first = report.result_uuids('tsk_1', 1)
        second = report.result_uuids('tsk_1', 2)
        self.assertNotEqual(first['group'], second['group'])
        self.assertTrue(first['group'].endswith(':group'))
        self.assertTrue(first['dm'].endswith(':dm'))


class WorkerCase(unittest.TestCase):
    """干活端：真的往 outbox 里排话（假工作台，不跑 dsh）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        # 刻意不建 git 仓库：干活端不许假设机器上有 git。
        self.store = Store(self.home / 'state.sqlite3')
        self.now = time.time()
        from bridge import worker as worker_module
        self.settings = worker_module.worker_config(
            {'worker': {}}, home=self.home, consumer='dsh-worker')
        self.fake = FakeWorkbench(result=WorkResult(
            ok=True, kind='done', text='三份 csv 汇总好了：128 行。', session_id='s',
            duration_seconds=90, output_bytes=10,
            artifacts=[{'path': 'summary.csv', 'bytes': 12}], handoff=None,
            work_dir=str(self.root)))
        self.worker = worker_module.Worker(self.store, self.settings,
                                           workbench_factory=lambda space: self.fake)
        self.worker.register()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # -- 夹具 ---------------------------------------------------------------
    def make_task(self, *, state='running'):
        """一条已批准的活；``om_card`` 是私聊里批准它的那条消息（私聊窗口凭它认出来）。"""
        inbox = self.store.connection
        inbox.execute("INSERT INTO inbox(id,state,received_at,chat_id,text,raw_json)"
                      " VALUES ('om_origin','done',?,'oc_group','把表汇总','{}')", (self.now,))
        inbox.execute("INSERT INTO inbox(id,state,received_at,chat_id,text,raw_json)"
                      " VALUES ('om_card','done',?,'%s','做','{}')" % DM_CHAT, (self.now,))
        task = self.store.draft_task('oc_group', '把表汇总', self.now, origin_mid='om_origin',
                                     message_id='om_origin')
        self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                hints_text='做，但只做①', now=self.now)
        if state == 'blocked':
            self.store.record_work_result(task['id'], state='blocked', now=self.now,
                                          note='卡住了', open_question='先把会话关掉')
        return task['id']

    def outbox(self, like):
        return self.store.connection.execute(
            'SELECT * FROM outbox WHERE uuid LIKE ? ORDER BY row_id', (like,)).fetchall()

    def events(self):
        return self.store.connection.execute(
            "SELECT * FROM events WHERE kind='work' ORDER BY id").fetchall()

    # -- 成功 ---------------------------------------------------------------
    def test_success_posts_one_group_message_that_references_the_original(self):
        task_id = self.make_task()
        self.worker.tick()
        rows = self.outbox('work-result:%')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['kind'], 'say')
        self.assertEqual(row['inbox_id'], 'om_origin')       # 挂在原消息下面
        self.assertEqual(row['chat_id'], 'oc_group')
        self.assertIn('干完了', row['body'])
        self.assertEqual(self.store.get_task(task_id)['state'], 'done')

    def test_success_does_not_send_a_private_message(self):
        self.make_task()
        self.worker.tick()
        self.assertEqual(self.outbox('work-result:%:dm%'), [])

    def test_only_the_attempt_of_this_run_is_used(self):
        self.make_task()
        self.worker.tick()
        uuids = [row['uuid'] for row in self.outbox('work-result:%')]
        self.assertEqual(uuids, ['work-result:%s:a1:group' % self.store.get_task(
            self.store.connection.execute("SELECT id FROM tasks").fetchone()['id'])['id']])

    # -- 失败与拦截 ---------------------------------------------------------
    def test_failure_posts_group_summary_and_dm_handoff(self):
        self.make_task()
        self.fake.result = WorkResult(ok=False, kind='timeout', text='超时了，卡在调用工具',
                                      session_id='s', duration_seconds=5, output_bytes=0,
                                      handoff=handoff_for(failure='超时了，卡在调用工具'),
                                      work_dir=str(self.root))
        self.worker.tick()
        group = self.store.connection.execute(
            "SELECT * FROM outbox WHERE uuid LIKE 'work-result:%:group'").fetchall()
        dm = self.store.connection.execute(
            "SELECT * FROM outbox WHERE uuid LIKE 'work-result:%:dm:%'").fetchall()
        self.assertEqual(len(group), 1)
        self.assertTrue(dm, '失败必须私聊一份交接单')
        self.assertEqual(dm[0]['chat_id'], DM_CHAT)
        self.assertIn('交接单', group[0]['body'])
        self.assertIn('接着干', dm[0]['body'])

    def test_blocked_sets_an_actionable_question(self):
        self.make_task()
        self.fake.result = WorkResult(ok=False, kind='lock_conflict', text='那个会话你正开着',
                                      session_id='s', duration_seconds=1, output_bytes=0,
                                      handoff=handoff_for(failure='会话被占用'),
                                      work_dir=str(self.root))
        self.worker.tick()
        task = self.store.get_task(self.store.connection.execute(
            "SELECT id FROM tasks").fetchone()['id'])
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('回我「执行」', task['open_question'])

    def test_blocked_group_message_tells_him_what_to_reply(self):
        self.make_task()
        self.fake.result = WorkResult(ok=False, kind='credential_missing', text='登录过期',
                                      session_id='s', duration_seconds=1, output_bytes=0,
                                      handoff=handoff_for(failure='登录过期'),
                                      work_dir=str(self.root))
        self.worker.tick()
        row = self.store.connection.execute(
            "SELECT * FROM outbox WHERE uuid LIKE 'work-result:%:group'").fetchone()
        self.assertIn('回我「执行」', row['body'])

    # -- 顺序与恢复 ---------------------------------------------------------
    def test_outbox_is_written_before_the_event_is_acked(self):
        self.make_task()
        original = self.store.ack_event
        seen = {}

        def spy(*args, **kwargs):
            seen['rows'] = self.store.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE uuid LIKE 'work-result:%'").fetchone()[0]
            seen['task_state'] = self.store.connection.execute(
                "SELECT state FROM tasks").fetchone()['state']
            return original(*args, **kwargs)

        self.store.ack_event = spy
        try:
            self.worker.tick()
        finally:
            self.store.ack_event = original
        self.assertGreaterEqual(seen.get('rows', 0), 1, 'ack 的时候话还没排进队列')
        self.assertEqual(seen.get('task_state'), 'done', 'ack 的时候账还没记')

    def test_result_is_on_disk_before_delivery(self):
        task_id = self.make_task()
        self.worker.tick()
        path = self.home / 'work' / task_id / 'result-a1.json'
        self.assertTrue(path.exists())
        record = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(record['state'], 'done')
        self.assertEqual(record['attempt'], 1)

    def test_crash_after_enqueue_does_not_duplicate_the_message(self):
        self.make_task()
        original = self.store.record_work_result

        def boom(*args, **kwargs):
            raise RuntimeError('crash after enqueue')

        self.store.record_work_result = boom
        try:
            with self.assertRaises(RuntimeError):
                self.worker.tick()
        finally:
            self.store.record_work_result = original
        self.assertEqual(len(self.outbox('work-result:%')), 1, '话应该已经排进去了')
        self.assertEqual(self.events()[0]['state'], 'leased', '崩溃时不该 ack')

        # 租约到期后重领：不许重跑，也不许重发
        self.store.connection.execute(
            "UPDATE events SET state='pending',consumer=NULL,claim_token=NULL,lease_until=0"
            " WHERE kind='work'")
        summary = self.worker.tick()
        self.assertEqual(summary.get('recovered'), True)
        self.assertEqual(len(self.outbox('work-result:%')), 1, '同一结论重放不该刷屏')
        self.assertEqual(self.store.get_task(
            self.store.connection.execute("SELECT id FROM tasks").fetchone()['id'])['state'],
            'done')

    # -- 接得上 -------------------------------------------------------------
    def test_redispatch_creates_the_next_attempt_event(self):
        task_id = self.make_task(state='blocked')
        result = self.store.redispatch_blocked(task_id, now=self.now, max_attempts=3)
        self.assertTrue(result['applied'])
        self.assertEqual(result['attempt'], 2)
        keys = [row['dedupe_key'] for row in self.events()]
        self.assertIn('work:' + task_id, keys)
        self.assertIn('work:%s:2' % task_id, keys)
        self.assertEqual(self.store.get_task(task_id)['state'], 'running')

    def test_redispatch_stops_at_the_attempt_limit(self):
        task_id = self.make_task(state='blocked')
        self.store.connection.execute("UPDATE tasks SET attempts=3 WHERE id=?", (task_id,))
        result = self.store.redispatch_blocked(task_id, now=self.now, max_attempts=3)
        self.assertTrue(result['exhausted'])
        task = self.store.get_task(task_id)
        self.assertEqual(task['state'], 'failed')
        self.assertIn('先别派了', task['steps'][-1]['note'])
        self.assertIsNone(task['open_question'])


class ExecuteCommandTests(unittest.TestCase):
    """`执行` 在没有待批卡时，去重派卡住的活——不新造命令词。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        config = {'target_open_id': OWNER, 'sending_enabled': True, 'self_chat_ids': ['oc_self'],
                  'worker': {'max_attempts': 3}}
        (self.home / 'config.json').write_text(json.dumps(config))
        self.service = Service(self.home, config)
        self.store = self.service.store
        self.now = time.time()
        self.store.connection.execute(
            "INSERT INTO inbox(id,state,received_at,chat_id,text,raw_json)"
            " VALUES ('om_origin','done',?,'oc_group','把表汇总','{}')", (self.now,))
        self.store.connection.execute(
            "INSERT INTO inbox(id,state,received_at,chat_id,text,raw_json)"
            " VALUES ('om_cmd','done',?,?,'执行','{}')", (self.now, DM_CHAT))
        task = self.store.draft_task('oc_group', '把表汇总', self.now, origin_mid='om_origin')
        self.store.approve_task(task['id'], approval_mid='om_cmd', approval_kind='command',
                                hints_text='做', now=self.now)
        self.store.record_work_result(task['id'], state='blocked', now=self.now,
                                      note='卡住了', open_question='关掉会话之后回我「执行」')
        self.task_id = task['id']

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_execute_redispatches_a_blocked_task(self):
        self.service._run_execute(None, 'om_cmd', self.now + 1, '执行')
        task = self.store.get_task(self.task_id)
        self.assertEqual(task['state'], 'running')
        self.assertEqual(task['attempts'], 2)
        events = self.store.connection.execute(
            "SELECT dedupe_key FROM events WHERE kind='work'").fetchall()
        self.assertIn('work:%s:2' % self.task_id, [row['dedupe_key'] for row in events])
        body = self.store.connection.execute(
            "SELECT body FROM outbox WHERE inbox_id='om_cmd' ORDER BY row_id DESC").fetchone()['body']
        self.assertIn('又派了一次', body)

    def test_execute_says_so_when_nothing_is_waiting(self):
        self.store.connection.execute("UPDATE tasks SET state='done'")
        self.service._run_execute(None, 'om_cmd', self.now + 2, '执行')
        body = self.store.connection.execute(
            "SELECT body FROM outbox WHERE inbox_id='om_cmd' ORDER BY row_id DESC").fetchone()['body']
        self.assertIn('没有等你批的卡，也没有卡住的活', body)
