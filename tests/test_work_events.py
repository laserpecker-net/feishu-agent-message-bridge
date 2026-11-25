"""PR-3 判据（一）：批准即派活——`work` 事件从哪来、长什么样、为什么不会派两次。

分两层：

* store 层：payload 六个字段、幂等、任务不在 open 时不派；
* 服务层：真的走 details 管道，证明"接线接上了"——app 机器人批不动、主人批完队列里
  就有一条只有干活端领得走的活。
"""

from tests.identity_doubles import UserModeDouble
import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge.store import Store
from tests.test_service import TaskCardCase, teach_message


class WorkEventStoreCases(unittest.TestCase):
    """账本层：一条批准 = 一件活，且只有一件。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def seed(self, mid='om_origin', text='把那三个 csv 汇总成一张表'):
        self.store.connection.execute(
            "INSERT INTO inbox(id,state,received_at,chat_id,text,raw_json) VALUES (?,?,?,?,?,'{}')",
            (mid, 'done', self.now, 'oc_group', text))

    def draft(self, title='汇总 csv', origin='om_origin', card=None):
        self.seed(origin)
        return self.store.draft_task('oc_group', title, self.now, origin_mid=origin,
                                     message_id=origin, card=card)

    def work_events(self):
        return [dict(row) for row in self.store.connection.execute(
            "SELECT * FROM events WHERE kind='work' ORDER BY id").fetchall()]

    # -- 1 ------------------------------------------------------------------
    CARD = ('集中汇总那三个 csv。\n\n我打算：① 拉三个文件 ② 按项目归类 ③ 发回群里\n'
            '会动到：只读文件\n\n👉 你只要回一个「做」我就开始；回「不做」我就不做了。\n\n'
            '做完你会看到：群里出现一张表')

    def test_approval_queues_one_work_event_with_the_full_task_package(self):
        task = self.draft(card=self.CARD)
        result = self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                         hints_text='做，但只做①', now=self.now)
        self.assertTrue(result['ok'] and result['applied'])
        self.assertTrue(result['work_queued'])
        events = self.work_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['dedupe_key'], 'work:' + task['id'])
        payload = json.loads(events[0]['payload'])
        self.assertEqual(payload['task_id'], task['id'])
        self.assertEqual(payload['chat_id'], 'oc_group')
        self.assertEqual(payload['origin_mid'], 'om_origin')
        self.assertEqual(payload['requirement'], self.CARD,
                         '任务书必须取自**卡片正文**：主人批的是那张卡，不是群里那条来源消息')
        self.assertEqual(payload['origin_text'], '把那三个 csv 汇总成一张表',
                         '来源消息原文只供人排查，不许被当成任务书喂给模型')
        self.assertEqual(payload['card_source'], 'card')
        self.assertEqual(payload['hints'], ['做，但只做①'], '主人批卡时补的话要原样带过去')

    def test_hints_is_always_a_list_even_when_the_owner_said_nothing_extra(self):
        task = self.draft()
        self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='command',
                                hints_text='', now=self.now)
        payload = json.loads(self.work_events()[0]['payload'])
        self.assertEqual(payload['hints'], [], '空也要空列表，不要 null——下游少一次判空')

    # -- 2 ------------------------------------------------------------------
    def test_replaying_the_same_approval_does_not_queue_a_second_work(self):
        task = self.draft()
        first = self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                        hints_text='做', now=self.now)
        again = self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                        hints_text='做', now=self.now)
        self.assertTrue(again['duplicate'])
        self.assertFalse(again['work_queued'], '重放不是"又派了一件活"')
        self.assertEqual(again['work_event_id'], first['work_event_id'])
        self.assertEqual(len(self.work_events()), 1)

    # -- 3 ------------------------------------------------------------------
    def test_a_task_that_is_not_open_does_not_queue_work(self):
        task = self.draft()
        self.store.reject_task(task['id'], approval_mid='om_no', now=self.now)
        result = self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                         hints_text='做', now=self.now)
        self.assertFalse(result['ok'])
        self.assertEqual(self.work_events(), [])

    def test_requirement_falls_back_to_the_title_when_no_card_was_stored(self):
        # 老任务（S3a 之前起草的）没有卡片正文：退回标题，仍然不为空、不许缺字段。
        task = self.store.draft_task('oc_group', '老任务：把报表整理一下', self.now,
                                     origin_mid='om_gone', message_id='om_gone')
        self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='quoted',
                                hints_text='做', now=self.now)
        payload = json.loads(self.work_events()[0]['payload'])
        self.assertEqual(payload['requirement'], '老任务：把报表整理一下')
        self.assertEqual(payload['card_source'], 'title')
        self.assertEqual(payload['origin_text'], '', '来源消息查不到就空着，不影响任务书')
        self.assertEqual(payload['origin_mid'], 'om_gone')

    def test_origin_text_is_never_used_as_the_task_package(self):
        """真机事故复现（2026-11-19）：来源消息是几分钟前另一句话，被判成任务书，模型干了另一件活。"""
        task = self.draft(title='自检：写 docs/e2e-worker-check.md 并提交', card=self.CARD)
        self.store.approve_task(task['id'], approval_mid='om_card', approval_kind='command',
                                hints_text='做', now=self.now)
        payload = json.loads(self.work_events()[0]['payload'])
        self.assertEqual(payload['requirement'], self.CARD, '任务书是卡片正文')
        self.assertEqual(payload['origin_text'], '把那三个 csv 汇总成一张表', '来源消息留档')
        self.assertNotEqual(payload['requirement'], payload['origin_text'],
                            '两者不许是同一段文字——那就是"拿来源消息当任务书"的老毛病')


class WorkEventServiceCases(TaskCardCase):
    """服务层：走真的 details 管道——批准之后队列里必须有一条只有干活端领得走的活。"""

    def work_events(self, service):
        return [dict(row) for row in service.store.connection.execute(
            "SELECT * FROM events WHERE kind='work' ORDER BY id").fetchall()]

    def test_quoted_approval_queues_a_work_event(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        self.deliver(service, teach_message('做，但只做①', mid='om_ans', parent_id=card_mid))
        events = self.work_events(service)
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]['payload'])['task_id'], task['id'])

    def test_an_app_bot_approval_queues_nothing(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        message = teach_message('做', mid='om_ans', parent_id=card_mid, sender='ou_app')
        message['sender']['sender_type'] = 'app'
        self.deliver(service, message)
        self.assertEqual(service.store.get_task(task['id'])['state'], 'open')
        self.assertEqual(self.work_events(service), [], 'app 批不动，也就不该派活')

    def test_a_vague_quoted_reply_queues_nothing(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        self.deliver(service, teach_message('我看看', mid='om_ans', parent_id=card_mid))
        self.assertEqual(self.work_events(service), [])

    def test_the_queued_work_is_only_claimable_by_a_worker(self):
        service = self.service(external=True)
        task, card_mid = self.draft_card(service)
        self.deliver(service, teach_message('做', mid='om_ans', parent_id=card_mid))
        now = time.time()
        service.store.register_consumer('replier-x', 'dsh', now, role='replier')
        with self.assertRaises(ValueError):
            service.store.claim_events('replier-x', now, kind='work')
        service.store.register_consumer('worker-x', 'dsh', now, role='worker')
        claimed = service.store.claim_events('worker-x', now, kind='work')
        self.assertEqual(len(claimed), 1)
        self.assertEqual(json.loads(claimed[0]['payload'])['task_id'], task['id'])
