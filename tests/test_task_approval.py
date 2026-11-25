"""任务卡与审批的 store 层判据（计划 §4 第 1–4 条；PR-1 范围）。

三态解析、私聊卡、引用接线属 PR-2/PR-3，不在这里。这里只钉账本本身：
起草写什么、批准怎么迁移状态并保留原话、重放为什么幂等、拒绝为什么是 cancelled。
"""

import json
import tempfile
import unittest
from pathlib import Path

from bridge.store import Store


class TaskApprovalStoreCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def draft(self, title='把三张表汇总成一张', origin_mid='om_origin'):
        return self.store.draft_task('oc_a', title, 100, origin_mid=origin_mid, message_id=origin_mid)

    # -- 1 ------------------------------------------------------------------
    def test_draft_task_writes_origin_mid_and_empty_hints(self):
        task = self.draft()
        self.assertEqual(task['state'], 'open')
        self.assertEqual(task['origin_mid'], 'om_origin')
        self.assertEqual(task['hints'], [], '主人还没说话，账本上不能先有"他说的"')
        self.assertIsNone(task['approval_mid'])
        self.assertIsNone(task['approved_at'])
        self.assertIsNone(task['started_at'])
        self.assertIsNone(task['executor'])
        # 「哪条消息引出的这活」是账本事实，不是内存里的一句话
        self.assertEqual(self.store.task_by_message('om_origin')['id'], task['id'])

    # -- 2 ------------------------------------------------------------------
    def test_approve_moves_open_to_running_and_keeps_original_words(self):
        task = self.draft()
        result = self.store.approve_task(task['id'], approval_mid='om_dm_1',
                                         approval_kind='quoted',
                                         hints_text='做，但只做①，别动表结构', now=200)
        self.assertTrue(result['ok'])
        self.assertTrue(result['applied'])
        approved = result['task']
        self.assertEqual(approved['state'], 'running')
        self.assertEqual(approved['approval_mid'], 'om_dm_1')
        self.assertEqual(approved['approval_kind'], 'quoted')
        self.assertEqual(approved['approved_at'], 200)
        self.assertEqual(approved['started_at'], 200)
        # 原话整段保留：不改写、不摘要、不拆成"意图"
        self.assertEqual([item['text'] for item in approved['hints']],
                         ['做，但只做①，别动表结构'])
        self.assertEqual(approved['hints'][0]['mid'], 'om_dm_1')
        self.assertNotIn('kind', approved['hints'][0], 'hints 只装主人原话，不夹带审计字段')
        # 落到库里（不是只在返回值里）
        self.assertEqual(self.store.get_task(task['id'])['hints'][0]['text'],
                         '做，但只做①，别动表结构')

    # -- 3 ------------------------------------------------------------------
    def test_approve_is_idempotent_and_does_not_duplicate_hints(self):
        task = self.draft()
        first = self.store.approve_task(task['id'], approval_mid='om_dm_1',
                                        approval_kind='quoted', hints_text='做', now=200)
        self.assertTrue(first['applied'])
        replay = self.store.approve_task(task['id'], approval_mid='om_dm_1',
                                         approval_kind='quoted', hints_text='做', now=300)
        self.assertTrue(replay['ok'])
        self.assertTrue(replay['duplicate'])
        self.assertFalse(replay['applied'])
        self.assertEqual(len(replay['task']['hints']), 1, '重放不许长出第二份 hints')
        self.assertEqual(replay['task']['approved_at'], 200, '重放不许刷新批准时间')
        # 换一条 mid 再"批"一次：状态机只有一个入口，已 running 就不再接受批准
        second = self.store.approve_task(task['id'], approval_mid='om_dm_2',
                                         approval_kind='unique_pending',
                                         hints_text='还有，只做②', now=400)
        self.assertFalse(second['ok'])
        self.assertFalse(second['applied'])
        self.assertEqual(len(second['task']['hints']), 1)

    # -- 4 ------------------------------------------------------------------
    def test_reject_word_moves_task_to_cancelled(self):
        task = self.draft()
        result = self.store.reject_task(task['id'], approval_mid='om_dm_reject', now=200)
        self.assertTrue(result['ok'])
        self.assertTrue(result['applied'])
        rejected = result['task']
        self.assertEqual(rejected['state'], 'cancelled')
        self.assertEqual(rejected['approval_mid'], 'om_dm_reject', '审计要能回答"谁叫停的"')
        self.assertIsNone(rejected['approved_at'], '他没批准，账本不能留下批准的痕迹')
        replay = self.store.reject_task(task['id'], approval_mid='om_dm_reject', now=300)
        self.assertTrue(replay['duplicate'])
        # 已结案的任务不能被一次迟到的批准翻回 running
        late = self.store.approve_task(task['id'], approval_mid='om_dm_late',
                                       approval_kind='quoted', hints_text='还是做吧', now=400)
        self.assertFalse(late['ok'])
        self.assertEqual(self.store.get_task(task['id'])['state'], 'cancelled')

    # -- B10：审计字段防回退 -------------------------------------------------
    def test_approval_kind_comes_from_the_column_not_from_hints(self):
        """「靠哪一档批的」是列里的审计事实：SQL 查得到，hints 元素里没有 kind。"""
        task = self.draft()
        approved = self.store.approve_task(task['id'], approval_mid='om_dm_1',
                                           approval_kind='unique_pending',
                                           hints_text='做', now=200)['task']
        stored = self.store.connection.execute(
            'SELECT approval_kind FROM tasks WHERE id=?', (task['id'],)).fetchone()[0]
        self.assertEqual(stored, 'unique_pending')
        self.assertEqual(approved['approval_kind'], stored)
        self.assertEqual(sorted(approved['hints'][0]), ['at', 'mid', 'text'],
                         'hints 元素回到 {text, at, mid}，不许再塞 kind')

    # -- 附加：状态机的旁路（同一条判据的另一面） ----------------------------
    def test_note_records_a_line_without_moving_state(self):
        task = self.draft()
        noted = self.store.note_task(task['id'], '记得先问权限', 200)
        self.assertEqual(noted['state'], 'open')
        self.assertEqual(noted['hints'], [], 'notes 不是主人的批准原话，不许混进 hints')
        self.assertEqual(noted['steps'][-1]['outcome'], 'note')
        self.assertEqual(noted['steps'][-1]['note'], '记得先问权限')

    def test_set_task_state_cannot_revive_a_closed_task(self):
        task = self.draft()
        self.store.reject_task(task['id'], approval_mid='om_dm_1', now=200)
        self.assertIsNone(self.store.set_task_state(task['id'], 'running', 300))
        self.assertEqual(self.store.get_task(task['id'])['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()


class ApprovalRuleCases(unittest.TestCase):
    """三态与卡片的**判定**（PR-2，计划 §4 的 B1/B2/B3/B4/B5/B6/B8）。

    这些是纯函数：不碰库、不发消息，所以判据能逐条单跑。
    """

    def test_b1_empty_answer_asks_again(self):
        from bridge import approval
        for text in ('', '   ', '\n\t'):
            self.assertEqual(approval.parse_answer(text), ('ask_again', ''), repr(text))
        decision = approval.judge(text='', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid=None, card_mid='om_card',
                                  pending_count=1)
        self.assertEqual(decision['verdict'], 'ask_again')
        self.assertIsNone(decision['kind'], '追问不推进任何状态')

    def test_b2_approval_requires_owner_sender_id(self):
        from bridge import approval
        decision = approval.judge(text='做', sender_id='ou_someone_else', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                  pending_count=1)
        self.assertEqual((decision['verdict'], decision['reason']), ('ignore', 'not_owner'))

    def test_b3_approval_requires_quoted_reply_or_unique_pending(self):
        from bridge import approval
        quoted = approval.judge(text='做', sender_id='ou_self', owner_id='ou_self',
                                chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                pending_count=2)
        self.assertEqual(quoted['verdict'], 'approve')
        self.assertEqual(quoted['kind'], 'quoted')
        unique = approval.judge(text='做', sender_id='ou_self', owner_id='ou_self',
                                chat_type='p2p', quoted_mid=None, card_mid='om_card',
                                pending_count=1)
        self.assertEqual((unique['verdict'], unique['kind']), ('approve', 'unique_pending'))
        self.assertEqual(approval.resolve_kind(quoted=True, pending_count=0), 'quoted')
        self.assertIsNone(approval.resolve_kind(quoted=False, pending_count=0))

    def test_b4_multiple_pending_without_quote_is_rejected(self):
        from bridge import approval
        decision = approval.judge(text='做', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid=None, card_mid='om_card',
                                  pending_count=2)
        self.assertEqual((decision['verdict'], decision['reason']), ('ambiguous', 'needs_quote'))
        self.assertIsNone(decision['kind'])

    def test_b5_approval_requires_private_chat(self):
        from bridge import approval
        decision = approval.judge(text='做', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='group', quoted_mid='om_card', card_mid='om_card',
                                  pending_count=1)
        self.assertEqual((decision['verdict'], decision['reason']), ('ignore', 'not_private'))

    def test_b6_app_sender_never_becomes_running(self):
        from bridge import approval
        decision = approval.judge(text='做', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                  pending_count=1, sender_is_app=True)
        self.assertEqual((decision['verdict'], decision['reason']), ('ignore', 'app_sender'))

    def test_b8_card_reads_like_a_person_talking(self):
        """卡片是主人唯一的交互界面：六段固定顺序，空行位置固定（2026-11-17 主人拍板）。"""
        from bridge import approval
        card = approval.render_card(
            summary='软件部群里有人让我把三个 csv 汇总成一张表。',
            why_me='要往群里发东西，得你点头我才敢发。',
            steps=['拉那三个文件', '按项目归类', '发回群里'],
            touches=['只读文件，不改任何东西'],
            done_when='群里出现一张表，每行有负责人和截止日')
        lines = card.split('\n')
        self.assertEqual(lines[0], '软件部群里有人让我把三个 csv 汇总成一张表。')
        self.assertEqual(lines[1], '要往群里发东西，得你点头我才敢发。')
        self.assertEqual(lines[2], '')
        self.assertEqual(lines[3], '我打算：① 拉那三个文件')
        self.assertEqual(lines[4], '② 按项目归类')
        self.assertEqual(lines[5], '③ 发回群里')
        self.assertEqual(lines[6], '会动到：只读文件，不改任何东西')
        self.assertEqual(lines[7], '')
        self.assertEqual(lines[8], approval.CARD_ACTION_LINE)
        self.assertEqual(lines[9], '')
        self.assertEqual(lines[10], '做完你会看到：群里出现一张表，每行有负责人和截止日')
        self.assertEqual(len(lines), 11, '顺序与空行是契约，多一行少一行都算改版')
        self.assertEqual(card.count('👉'), 1, '全卡只允许一行动作提示')

    def test_b8b_steps_missing_still_says_something_human(self):
        from bridge import approval
        card = approval.render_card(summary='群里有人问我要一份清单', why_me='要往外发东西，得你点头',
                                    steps=[], touches=[], done_when='群里出现那份清单')
        self.assertIn('我打算：我先看清楚再动手', card)
        self.assertIn('会动到：不动任何东西（只读）', card)

    def test_b8c_system_words_are_refused_with_a_human_reason(self):
        """模型用系统词写卡 = 还是站在系统那头说话：退回重写，报错文案给的是人话。"""
        from bridge import approval
        base = dict(summary='群里有人让我拉一份清单', why_me='要往外发，得你点头',
                    steps=['拉清单'], touches=['只读'], done_when='群里出现清单')
        for word in approval.BANNED_WORDS:
            payload = dict(base, steps=['拉清单，顺便看%s' % word])
            with self.assertRaises(approval.CardError) as caught:
                approval.render_card(**payload)
            message = str(caught.exception)
            self.assertIn(word, message)
            self.assertIn('大白话', message)

    def test_b8d_truncation_keeps_every_step_and_the_action_line(self):
        from bridge import approval
        card = approval.render_card(
            summary='长' * 400, why_me='因为' * 300, steps=['步' * 300, '二' * 60],
            touches=['碰' * 300], done_when='好' * 300)
        self.assertLessEqual(len(card), approval.CARD_LIMIT)
        self.assertIn(approval.TRUNCATED, card)
        self.assertIn(approval.CARD_ACTION_LINE, card, '动作那行不参与截断')
        self.assertIn('②', card, '按行剪：后面的步骤不能被整条剪掉')
        for label in (approval.CARD_LABEL_PLAN, approval.CARD_LABEL_TOUCHES,
                      approval.CARD_LABEL_DONE):
            self.assertIn(label, card)

    def test_b8e_missing_pieces_are_refused_with_plain_reason(self):
        from bridge import approval
        cases = [dict(summary='', why_me='因为', steps=['做'], touches=[], done_when='好'),
                 dict(summary='干活', why_me='', steps=['做'], touches=[], done_when='好'),
                 dict(summary='干活', why_me='因为', steps=['做'], touches=[], done_when=''),
                 dict(summary='干活', why_me='因为',
                      steps=['1', '2', '3', '4', '5', '6'], touches=[], done_when='好')]
        for payload in cases:
            with self.assertRaises(approval.CardError):
                approval.render_card(**payload)

    def test_reject_words_are_matched_before_approval_words(self):
        """拒绝词表逐个覆盖：单独一个词就是拒绝；「做，但不要 X」是批准带约束。"""
        from bridge import approval
        for word in approval.REJECT_WORDS:
            self.assertEqual(approval.parse_answer(word)[0], 'reject', word)
        self.assertEqual(approval.parse_answer('不做')[0], 'reject')
        self.assertEqual(approval.parse_answer('做，但只做①'), ('approve', '做，但只做①'))
        self.assertEqual(approval.parse_answer('做，但不要动生产')[0], 'approve')
        self.assertEqual(approval.parse_answer('先别做，等下周')[0], 'reject')

    def test_quoted_card_without_decision_word_asks_again(self):
        """引用只证明"在答这张卡"，不证明"同意做"（2026-11-17 收紧）。"""
        from bridge import approval
        for text in ('我看看', '好复杂', '等我问下同事'):
            decision = approval.judge(text=text, sender_id='ou_self', owner_id='ou_self',
                                      chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                      pending_count=1)
            with self.subTest(text=text):
                self.assertEqual((decision['verdict'], decision['reason']),
                                 ('ask_again', 'no_decision_word'))
                self.assertIsNone(decision['kind'], '追问不推进、不记 hints')
                self.assertEqual(decision['hints'], '')

    def test_quoted_card_with_modification_only_asks_again(self):
        """「把第 2 步改成先备份」是想法不是表态：不推进，追问一次。"""
        from bridge import approval
        decision = approval.judge(text='把第2步改成先备份', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                  pending_count=1)
        self.assertEqual((decision['verdict'], decision['reason']),
                         ('ask_again', 'no_decision_word'))

    def test_quoted_card_with_decision_and_modification_approves(self):
        """表态 + 改动 = 批准，整句进 hints（既有语义不变）。"""
        from bridge import approval
        words = '做，第2步先备份'
        decision = approval.judge(text=words, sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                  pending_count=3)
        self.assertEqual((decision['verdict'], decision['kind']), ('approve', 'quoted'))
        self.assertEqual(decision['hints'], words, '原话整段保留')

    def test_unique_pending_without_decision_word_asks_again(self):
        from bridge import approval
        decision = approval.judge(text='我看看', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid=None, card_mid='om_card',
                                  pending_count=1)
        self.assertEqual((decision['verdict'], decision['reason']),
                         ('ask_again', 'no_decision_word'))
        self.assertIsNone(decision['kind'])

    def test_decision_words_are_boundary_aware(self):
        """单字决定词只在词边界上算：避免「运行」「执行」被当成「行」。"""
        from bridge import approval
        for text in ('再运行测试', '执行完告诉我', '我看看进度'):
            with self.subTest(text=text):
                self.assertFalse(approval.has_decision_word(text), text)
        for text, expected in (('行吧', 'approve'), ('准了', 'approve'), ('好的', 'approve'),
                               ('ok', 'approve'), ('不批', 'reject'), ('驳回', 'reject'),
                               ('这个功能不行', 'reject')):
            with self.subTest(text=text):
                self.assertEqual(approval.parse_answer(text)[0], expected)
        self.assertFalse(approval.has_decision_word('token 过期了'))


class ApprovalLedgerCases(unittest.TestCase):
    """批准落到账本上：走全链路（B9）、过期作废（B7）、审计来源（C1）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def draft(self, now=100, title='把三张表汇总成一张'):
        return self.store.draft_task('oc_a', title, now, origin_mid='om_origin',
                                     message_id='om_origin')

    def test_b9_quoted_zuo_dan_zhi_zuo_yi_reply_runs_with_original_words(self):
        """设计 §12 原文判据：私聊引用回复「做，但只做①」→ running 且 hints 含原话。"""
        from bridge import approval
        task = self.draft()
        words = '做，但只做①'
        decision = approval.judge(text=words, sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid='om_card', card_mid='om_card',
                                  pending_count=3)
        self.assertEqual(decision['verdict'], 'approve')
        result = self.store.approve_task(task['id'], approval_mid='om_dm_1',
                                         approval_kind=decision['kind'],
                                         hints_text=decision['hints'], now=200)
        self.assertTrue(result['applied'])
        approved = result['task']
        self.assertEqual(approved['state'], 'running')
        self.assertEqual(approved['hints'][0]['text'], words, '原话整段保留，不改写不摘要')
        self.assertEqual(approved['approval_kind'], 'quoted')

    def test_c1_unique_pending_approval_without_quote_records_kind(self):
        from bridge import approval
        task = self.draft()
        decision = approval.judge(text='做', sender_id='ou_self', owner_id='ou_self',
                                  chat_type='p2p', quoted_mid=None, card_mid='om_card',
                                  pending_count=1)
        self.assertEqual(decision['kind'], 'unique_pending')
        result = self.store.approve_task(task['id'], approval_mid='om_dm_2',
                                         approval_kind=decision['kind'],
                                         hints_text=decision['hints'], now=200)
        self.assertEqual(result['task']['state'], 'running')
        self.assertEqual(result['task']['approval_kind'], 'unique_pending')

    def test_b7_card_expires_after_ttl_and_announces(self):
        from bridge import approval
        task = self.draft(now=1000)
        announced = []
        expired = approval.expire_cards(self.store, 1000 + 31 * 60,
                                       ttl_seconds=30 * 60, announce=announced.append)
        self.assertEqual([item['id'] for item in expired], [task['id']])
        self.assertEqual(announced[0]['id'], task['id'], '过点要回群说一句')
        stored = self.store.get_task(task['id'])
        self.assertEqual(stored['state'], 'cancelled')
        self.assertIsNone(stored['approval_mid'], '没人批过，账本上不许有批准的痕迹')
        self.assertEqual(stored['steps'][-1]['note'][:12], 'card_expired')
        # 没过期的卡一张都不许动
        fresh = self.draft(now=2000, title='另一件')
        later = approval.expire_cards(self.store, 2000 + 60, ttl_seconds=1800)
        self.assertEqual(later, [])
        self.assertEqual(self.store.get_task(fresh['id'])['state'], 'open')


class TaskCardCliCases(unittest.TestCase):
    """CLI 这一层：起草（建账 + 发卡 + 挂消息）、批准、拒绝、记账、过期作废。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / 'config.json').write_text(json.dumps(
            {'target_open_id': 'ou_self', 'sending_enabled': True, 'signature': '署名'}))
        self.store = Store(self.home / 'state.sqlite3')
        self.store.close()
        self.card = {'summary': '把周报汇总成一张表', 'why_me': '我有群读取权限',
                     'steps': ['拉最近 7 天消息', '按项目归类'], 'touches': ['群消息只读'],
                     'done_when': '群里出现一张表'}

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, argv, stdin=''):
        import contextlib
        import io
        from unittest import mock
        from bridge import cli
        stream = io.StringIO()
        fake = mock.Mock()
        fake.send_dm.return_value = {'message_id': 'om_card_1', 'chat_id': 'oc_dm'}
        with mock.patch('sys.stdin', io.StringIO(stdin)), \
                mock.patch('bridge.transport.Transport', return_value=fake), \
                contextlib.redirect_stdout(stream):
            code = cli._task(str(self.home), argv)
        return code, json.loads(stream.getvalue() or '{}'), fake

    def test_draft_creates_task_sends_card_and_attaches_it(self):
        code, report, fake = self.run_cli(
            ['draft', '--chat', 'oc_a', '--origin-mid', 'om_origin'], stdin=json.dumps(self.card))
        self.assertEqual(code, 0)
        self.assertTrue(report['sent'])
        self.assertEqual(report['card_mid'], 'om_card_1')
        body = fake.send_dm.call_args[0][1]
        from bridge import approval
        self.assertIn(approval.CARD_ACTION_LINE, body, '私聊卡必须带那句"回做或不回"')
        for label in (approval.CARD_LABEL_PLAN, approval.CARD_LABEL_TOUCHES,
                      approval.CARD_LABEL_DONE):
            self.assertIn(label, body)
        self.assertIn('①', body)
        self.assertNotIn('【活】', body, '旧标签已经删了，不许回来')
        self.assertLessEqual(len(report['task']['title']), 200)
        store = Store(self.home / 'state.sqlite3')
        try:
            task = store.get_task(report['task']['id'])
            self.assertEqual(task['state'], 'open')
            self.assertEqual(task['origin_mid'], 'om_origin')
            self.assertEqual(store.task_by_message('om_card_1')['id'], task['id'],
                             '卡片本身就是这条任务的消息：本人引用它时任务能对上号')
            self.assertEqual(report['task']['waiting']['for'], 'owner')
        finally:
            store.close()

    def test_draft_refuses_an_incomplete_card_without_touching_the_ledger(self):
        broken = dict(self.card)
        broken.pop('done_when')
        code, report, fake = self.run_cli(
            ['draft', '--chat', 'oc_a', '--origin-mid', 'om_origin'], stdin=json.dumps(broken))
        self.assertEqual(code, 2)
        self.assertFalse(report['ok'])
        self.assertEqual(report['stage'], 'render')
        fake.send_dm.assert_not_called()
        store = Store(self.home / 'state.sqlite3')
        try:
            self.assertEqual(store.tasks(), [], '没想清楚就不许落账')
        finally:
            store.close()

    def test_approve_runs_and_keeps_the_words(self):
        self.run_cli(['draft', '--chat', 'oc_a', '--origin-mid', 'om_origin'],
                     stdin=json.dumps(self.card))
        store = Store(self.home / 'state.sqlite3')
        task = store.tasks()[0]
        store.close()
        code, report, _ = self.run_cli(['approve', task['id'], '--mid', 'om_dm_1',
                                        '--kind', 'unique_pending'], stdin='做，但只做①')
        self.assertEqual(code, 0)
        self.assertTrue(report['applied'])
        self.assertEqual(report['task']['state'], 'running')
        self.assertEqual(report['task']['audit']['hints'][0]['text'], '做，但只做①')
        self.assertEqual(report['task']['audit']['approval_kind'], 'unique_pending')

    def test_approve_refuses_an_empty_answer(self):
        self.run_cli(['draft', '--chat', 'oc_a', '--origin-mid', 'om_origin'],
                     stdin=json.dumps(self.card))
        store = Store(self.home / 'state.sqlite3')
        task = store.tasks()[0]
        store.close()
        code, report, _ = self.run_cli(['approve', task['id'], '--mid', 'om_dm_1',
                                        '--kind', 'quoted'], stdin='   ')
        self.assertEqual(code, 4, '空答复不推进，也不当成用法错误')
        self.assertEqual(report['verdict'], 'ask_again')
        store = Store(self.home / 'state.sqlite3')
        try:
            self.assertEqual(store.get_task(task['id'])['state'], 'open', '一个字都没改')
        finally:
            store.close()

    def test_reject_and_note(self):
        self.run_cli(['draft', '--chat', 'oc_a', '--origin-mid', 'om_origin'],
                     stdin=json.dumps(self.card))
        store = Store(self.home / 'state.sqlite3')
        task = store.tasks()[0]
        store.close()
        code, report, _ = self.run_cli(['note', task['id']], stdin='先问权限')
        self.assertEqual(code, 0)
        self.assertEqual(report['state'], 'open')
        self.assertEqual(report['steps'][-1]['note'], '先问权限')
        code, report, _ = self.run_cli(['reject', task['id'], '--mid', 'om_dm_no'])
        self.assertEqual(code, 0)
        self.assertEqual(report['task']['state'], 'cancelled')

    def test_expire_cancels_and_queues_one_line_to_the_group(self):
        store = Store(self.home / 'state.sqlite3')
        task = store.draft_task('oc_a', '老卡', 1000, origin_mid='om_old')
        store.close()
        code, report, _ = self.run_cli(['expire', '--ttl-minutes', '30'])
        self.assertEqual(code, 0)
        self.assertEqual(report['count'], 1)
        store = Store(self.home / 'state.sqlite3')
        try:
            self.assertEqual(store.get_task(task['id'])['state'], 'cancelled')
            rows = store.connection.execute(
                "SELECT chat_id,kind,state,body FROM outbox WHERE kind='say'").fetchall()
        finally:
            store.close()
        self.assertEqual(len(rows), 1, '过点要在群里留一句话，且只留一句')
        self.assertEqual(rows[0]['chat_id'], 'oc_a')
        self.assertEqual(rows[0]['state'], 'queued', '走 outbox：不绕发送层')
        self.assertIn('过点了', rows[0]['body'])


class CardPromptCases(unittest.TestCase):
    """起草口径只在 prompt 说一次（PR-2 第 3 条）：群里给协议，私聊不给。"""

    def build(self, chat_type):
        import agent_prompt
        return agent_prompt.build_prompt({'message_id': 'om_1', 'text': '能不能帮我做个表',
                                          'chat_id': 'oc_a', 'chat_type': chat_type})

    def test_group_prompt_states_the_card_contract(self):
        prompt = self.build('group')
        for key in ('task_card', '"summary"', '"why_me"', '"steps"', '"touches"', '"done_when"'):
            self.assertIn(key, prompt, key)
        self.assertIn('不要再起草', prompt, '有 open 的卡时不重复起草，必须写进判据')
        self.assertIn('五段缺一段', prompt)

    def test_private_chat_does_not_get_the_card_protocol(self):
        prompt = self.build('p2p')
        self.assertNotIn('task_card', prompt)
        self.assertNotIn('什么时候起草任务卡', prompt)
