"""A 方案判据：主人自己下的写活 = 免批准，但**必须**走干活端。

事故背景（2026-11-19，生产真事）：主人在群里下了一道写活（把 README 某节重写并提交），
回话端直接替他改了仓库、还提交了 —— 没有批准、没有工作树隔离、账本里没有任何痕迹。
本人下令确实不必再问他一遍，但"谁干的、改了哪、能不能回滚"这三件事不能丢。

所以判据分两半：
* 派活这件事（本文件）：``task direct`` 建账 → 按 ``owner_direct`` 立即批准 → 产生 work 事件
  → 群里回一句回执，**不发卡、不私聊打扰**；
* 干活与回执：由 PR-3/PR-4 已有的链路负责（见 test_worker.py / test_report_back.py）。
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_prompt import card_section
from bridge import cli
from bridge.store import Store
import consumer_safety


CARD = {
    'summary': '在 README 里加一行说明并提交',
    'why_me': '要往仓库里写文件，按规矩该你点头。',
    'steps': ['打开 README', '加一行说明', '提交'],
    'touches': ['README.md'],
    'done_when': '仓库里多一次提交，README 多一行',
}
OWNER = 'ou_owner'


class OwnerDirectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / 'config.json').write_text(json.dumps({
            'target_open_id': OWNER,
            'self_chat_ids': ['oc_group'],
            'target_chat_id': 'oc_dm',
        }), encoding='utf-8')
        store = Store(str(self.home / 'state.sqlite3'))
        store.close()
        self.now = time.time()

    def tearDown(self):
        self.temp.cleanup()

    # --- 夹具 ---------------------------------------------------------------
    def open_store(self):
        return Store(str(self.home / 'state.sqlite3'))

    def seed_message(self, mid='om_owner', sender=OWNER, sender_type='user', text='把这节改掉并提交'):
        store = self.open_store()
        try:
            store.connection.execute(
                'INSERT INTO inbox(id,state,received_at,chat_id,sender_id,sender_type,text,raw_json)'
                " VALUES(?,?,?,?,?,?,?,'{}')",
                (mid, 'done', self.now, 'oc_group', sender, sender_type, text))
        finally:
            store.close()
        return mid

    def run_task(self, *argv, stdin=None):
        stream = io.StringIO()
        with patch('sys.argv', ['feishu-bridge', 'task'] + [str(a) for a in argv]), \
                contextlib.redirect_stdout(stream):
            if stdin is None:
                code = cli.main(str(self.home))
            else:
                with patch('sys.stdin', io.StringIO(stdin)):
                    code = cli.main(str(self.home))
        out = stream.getvalue()
        return code, (json.loads(out) if out.strip() else None)

    def direct(self, mid='om_owner', card=None):
        return self.run_task('direct', '--chat', 'oc_group', '--origin-mid', mid,
                             stdin=json.dumps(card or CARD, ensure_ascii=False))

    # --- 1. 主人写活：免批、派活、群里回一句、不发卡 --------------------------
    def test_owner_write_is_dispatched_without_a_card(self):
        mid = self.seed_message()
        with patch.object(cli, '_send_owner_notice', side_effect=AssertionError('不许发卡')):
            code, result = self.direct(mid)
        self.assertEqual(code, 0, result)
        self.assertTrue(result['ok'])
        self.assertFalse(result['duplicate'])
        task = result['task']
        self.assertEqual(task['state'], 'running')
        self.assertEqual(task['approval_kind'], 'owner_direct')
        self.assertEqual(task['approval_mid'], mid)
        self.assertTrue(task['approved_at'])
        self.assertTrue(task['started_at'], '批准即开工：started_at 必须有')
        self.assertTrue(result['work_event_id'], '必须派出一条 work 事件')
        self.assertTrue(result['work_queued'])
        store = self.open_store()
        try:
            events = store.connection.execute(
                "SELECT state,payload FROM events WHERE kind='work'").fetchall()
            self.assertEqual(len(events), 1)
            payload = json.loads(events[0]['payload'])
            self.assertEqual(payload['task_id'], task['id'])
            self.assertIn('README', payload['requirement'],
                          '任务书取卡片正文，不是来源消息原文')
            # 群里那一句回执：走 outbox 的 say，挂在原消息下
            says = store.connection.execute(
                "SELECT inbox_id,chat_id,uuid FROM outbox WHERE kind='say'").fetchall()
            self.assertEqual(len(says), 1)
            self.assertEqual(says[0]['inbox_id'], mid)
            self.assertEqual(says[0]['chat_id'], 'oc_group')
            self.assertIn(task['id'], says[0]['uuid'])
            # 没有任何私聊卡片（卡片的 uuid 前缀是 task-card:）
            cards = store.connection.execute(
                "SELECT 1 FROM outbox WHERE uuid LIKE 'task-card:%'").fetchall()
            self.assertEqual(cards, [])
        finally:
            store.close()

    # --- 2. 同一条消息重投：只有一个任务 -------------------------------------
    def test_the_same_message_never_dispatches_twice(self):
        mid = self.seed_message()
        first = self.direct(mid)
        second = self.direct(mid)
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertTrue(second[1]['duplicate'])
        self.assertEqual(second[1]['task']['id'], first[1]['task']['id'])
        store = self.open_store()
        try:
            self.assertEqual(store.connection.execute(
                'SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE kind='work'").fetchone()[0], 1)
        finally:
            store.close()

    # --- 3. 权限边界：别人 / app 身份都不许 ----------------------------------
    def test_a_colleague_cannot_be_dispatched_as_owner_direct(self):
        mid = self.seed_message(sender='ou_colleague', text='帮我把 README 改一下')
        code, result = self.direct(mid)
        self.assertEqual(code, 2)
        self.assertFalse(result['ok'])
        self.assertEqual(result['stage'], 'guard')
        store = self.open_store()
        try:
            self.assertEqual(store.connection.execute(
                'SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        finally:
            store.close()

    def test_an_app_sender_can_never_become_owner_direct(self):
        mid = self.seed_message(sender=OWNER, sender_type='app')
        code, result = self.direct(mid)
        self.assertEqual(code, 2)
        self.assertEqual(result['stage'], 'guard')

    # --- 4. 没想清楚就不派（缺 touches / done_when）--------------------------
    def test_missing_touches_or_done_when_is_refused_not_guessed(self):
        mid = self.seed_message()
        for missing in ('touches', 'done_when'):
            card = dict(CARD)
            card[missing] = [] if missing == 'touches' else ''
            code, result = self.direct(mid, card)
            self.assertEqual(code, 2, '%s 缺了却派了活' % missing)
            self.assertEqual(result['stage'], 'validate')
        store = self.open_store()
        try:
            self.assertEqual(store.connection.execute(
                'SELECT COUNT(*) FROM tasks').fetchone()[0], 0, '没想清楚就不许建账')
        finally:
            store.close()

    def test_card_render_failure_does_not_create_a_task(self):
        mid = self.seed_message()
        card = dict(CARD)
        card['summary'] = '账本里加一列'      # 系统词：卡片渲染会拒
        code, result = self.direct(mid, card)
        self.assertEqual(code, 2)
        self.assertEqual(result['stage'], 'render')
        store = self.open_store()
        try:
            self.assertEqual(store.connection.execute(
                'SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        finally:
            store.close()

    # --- 5. 回执发不出去：活照派，但如实说 --------------------------------
    def test_a_failed_receipt_does_not_undo_the_dispatch(self):
        mid = self.seed_message()
        with patch.object(Store, 'announce', side_effect=sqlite3.OperationalError('disk full')):
            code, result = self.direct(mid)
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertFalse(result['receipt']['ok'])
        self.assertIn('disk full', result['receipt']['error'])
        store = self.open_store()
        try:
            self.assertEqual(store.get_task(result['task']['id'])['state'], 'running')
        finally:
            store.close()


    def test_executor_audit_is_written_by_the_worker_on_the_same_task(self):
        """免批不等于免审计：干活端领活时必须把 executor 写进这条任务（S4 余项判据）。

        写入点是 ``bridge/worker.py`` 的 tick（``set_task_executor('dsh-web@<机器名>')``），
        本文件只钉住"这条 owner_direct 任务也吃同一套审计"。
        """
        mid = self.seed_message()
        code, result = self.direct(mid)
        self.assertEqual(code, 0)
        task_id = result['task']['id']
        store = self.open_store()
        try:
            self.assertIsNone(store.get_task(task_id)['executor'], '派活那一刻还没人干')
            store.set_task_executor(task_id, 'dsh-web@test-machine', self.now)
            task = store.get_task(task_id)
            self.assertEqual(task['executor'], 'dsh-web@test-machine')
            self.assertEqual(task['approval_kind'], 'owner_direct')
        finally:
            store.close()


class OwnerDirectParsingTests(unittest.TestCase):
    """答复 JSON 这一侧：direct_work 的形状与 task_card 一致，但多一道硬门槛。"""

    def reply(self, **fields):
        body = {'answer': '收到，我派人去干。'}
        body.update(fields)
        return consumer_safety.finalize_reply(json.dumps(body, ensure_ascii=False))

    def test_direct_work_uses_exactly_the_same_keys_as_the_card(self):
        card = consumer_safety.normalize_task_card(CARD)
        direct = consumer_safety.normalize_direct_work(CARD)
        self.assertEqual(set(card), set(direct))
        self.assertEqual(card, direct)

    def test_direct_work_without_touches_is_dropped_loudly(self):
        card = dict(CARD)
        card['touches'] = []
        reply = self.reply(direct_work=card)
        self.assertIsNone(reply['direct_work'])
        self.assertIn('direct_work', reply['dropped'], '丢掉要说出来，不能静默')

    def test_card_and_direct_work_together_are_contradictory(self):
        reply = self.reply(task_card=CARD, direct_work=CARD)
        self.assertTrue(reply['task_card'], '卡片该留')
        self.assertIsNone(reply['direct_work'], '又请他点头又直接派活，只能选一个')
        self.assertIn('direct_work', reply['dropped'])

    def test_direct_work_alone_keeps_the_answer(self):
        reply = self.reply(direct_work=CARD)
        self.assertEqual(reply['answer'], '收到，我派人去干。')
        self.assertIsNotNone(reply['direct_work'])
        self.assertEqual(reply['dropped'], [])

    def test_skip_and_direct_work_cannot_coexist(self):
        with self.assertRaises(ValueError):
            consumer_safety.finalize_reply(json.dumps(
                {'skip': '这条不用回', 'direct_work': CARD}, ensure_ascii=False),
                allow_skip=True)


class DispatchWiringTests(unittest.TestCase):
    """消费者侧接线：答复里的 direct_work 必须走 `task direct` 这条 CLI，不许另开实现。"""

    def test_dispatch_direct_calls_the_task_direct_command(self):
        import consumer_runtime
        calls = []

        def call(argv, body=None):
            calls.append((argv, body))
            return {'ok': True, 'task': {'id': 'tsk_1'}}

        event = {'id': 7, 'payload': {'chat_id': 'oc_group', 'message_id': 'om_1'}}
        result = consumer_runtime.dispatch_direct(call, event, CARD)
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][0], ['task', 'direct', '--chat', 'oc_group',
                                       '--origin-mid', 'om_1'])
        self.assertEqual(json.loads(calls[0][1]), CARD)

    def test_dispatch_direct_reports_missing_payload_instead_of_raising(self):
        import consumer_runtime
        result = consumer_runtime.dispatch_direct(lambda *a, **k: {}, {'payload': {}}, CARD)
        self.assertFalse(result['ok'])
        self.assertIn('payload', result['error'])


class OwnerDirectPromptTests(unittest.TestCase):
    """prompt 是唯一一处判据：本人下令 vs 别人请求，必须分别说清。"""

    def test_group_prompt_explains_direct_work(self):
        text = card_section('group')
        self.assertIn('direct_work', text)
        self.assertIn('本人自己下的写活', text)
        self.assertIn('不许自己动手改东西', text)
        self.assertIn('只读的、一句话能答的，仍然直接答', text)

    def test_private_chat_has_no_dispatch_protocol(self):
        self.assertEqual(card_section('p2p'), '')
