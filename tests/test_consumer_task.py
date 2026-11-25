"""任务台账（L1）的消费者侧：答复里的 task 更新 → 运行时 flag → prompt 里的台账段。

契约在 CONTRACTS.md：payload 带 ``task``/``tasks``，答复 JSON 可带一个 ``task`` 对象；
坏字段只丢那个字段（台账写坏不能连累正文），运行时把更新拆成 ``--task-*`` 开关。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import threading
import time
import unittest

import consumer_runtime
import consumer_safety
from agent_prompt import TASK_PROTOCOL, build_prompt

TOKEN = 'a' * 32


class TaskUpdateTests(unittest.TestCase):
    """``finalize_reply`` 里的台账更新：逐字段校验、绝不抛错、旧形状只多一个 task 键。"""

    def test_valid_task_object_passes_through(self):
        reply = consumer_safety.finalize_reply(json.dumps({
            'answer': '读完两个 excel，第三个打不开。',
            'task': {'id': 'tsk_ab12cd34ef56', 'state': 'running',
                     'note': '  读完 2/3 个 excel，缺 3 月数据  ',
                     'artifacts': [{'path': '/tmp/out.xlsx', 'kind': 'file'}],
                     'open_question': '3 月的数据在哪个目录？'}}))
        self.assertEqual(reply['task'], {
            'id': 'tsk_ab12cd34ef56', 'state': 'running',
            'note': '读完 2/3 个 excel，缺 3 月数据',
            'artifacts': [{'path': '/tmp/out.xlsx', 'kind': 'file'}],
            'open_question': '3 月的数据在哪个目录？'})
        self.assertEqual(reply['answer'], '读完两个 excel，第三个打不开。')
        self.assertIsNone(reply['ask_owner'])

    def test_invalid_state_is_dropped_but_the_rest_survives(self):
        reply = consumer_safety.finalize_reply(json.dumps({
            'answer': 'x', 'task': {'state': 'finished', 'note': '做到一半'}}))
        self.assertEqual(reply['task'], {'note': '做到一半'})
        # 状态全坏就等于没有可用的更新：不臆测成 running，也不报错。
        self.assertIsNone(consumer_safety.normalize_task_update({'state': ''}))
        self.assertIsNone(consumer_safety.normalize_task_update({'state': 7}))

    def test_oversized_note_and_question_are_dropped_field_by_field(self):
        reply = consumer_safety.finalize_reply(json.dumps({
            'answer': 'x',
            'task': {'state': 'running', 'note': 'a' * (consumer_safety.MAX_TASK_NOTE_CHARS + 1),
                     'open_question': 'b' * (consumer_safety.MAX_TASK_QUESTION_CHARS + 1)}}))
        self.assertEqual(reply['task'], {'state': 'running'})

    def test_plain_string_artifacts_and_unknown_keys(self):
        reply = consumer_safety.finalize_reply(json.dumps({
            'answer': 'x',
            'task': {'artifacts': ['/tmp/a.csv', {'path': '/tmp/b.csv', 'sha256': 'c' * 64},
                                   {'kind': 'file'}, 42],
                     'created_at': 'ignored', 'chat_id': 'oc_ignored'}}))
        self.assertEqual(reply['task']['artifacts'],
                         [{'path': '/tmp/a.csv'},
                          {'path': '/tmp/b.csv', 'sha256': 'c' * 64}])

    def test_garbage_task_never_raises_and_never_fails_the_answer(self):
        for value in (None, 'tsk_1', [], 42, {'state': {'nested': True}}, {'id': {'x': 1}},
                      {'artifacts': 'not-a-list'}, {'artifacts': [{'path': 5}]},
                      {'note': '\x00'}, {'id': 'tsk_' + 'a' * 100}):
            with self.subTest(value=repr(value)):
                reply = consumer_safety.finalize_reply(json.dumps({'answer': '正文', 'task': value}))
                self.assertEqual(reply['answer'], '正文')
                self.assertIsNone(reply['task'])

    def test_task_id_that_looks_like_a_flag_is_dropped(self):
        # id 会被拼进 `--task-id` 的 argv：`-r` 这种值等于让模型自己加开关。
        reply = consumer_safety.finalize_reply(json.dumps({
            'answer': '正文', 'task': {'id': '-r', 'state': 'running'}}))
        self.assertEqual(reply['task'], {'state': 'running'})

    def test_reply_without_task_has_the_same_old_fields_plus_none(self):
        for text in (json.dumps({'answer': '直接答上了'}), '直接答上了'):
            with self.subTest(text=text):
                result = consumer_safety.finalize_reply(text)
                # 2026-11-19 A 方案：答复里多了 direct_work（主人自己下的写活免批准直接派），
                # 形状判据跟着长一个键——语义扩展，不是放宽。
                self.assertEqual(set(result), {'answer', 'ask_owner', 'task', 'task_card', 'direct_work',
                                      'teach', 'teach_revert', 'remind', 'remind_cancel', 'skip',
                                      'dropped'})
                self.assertEqual(result['answer'], '直接答上了')
                self.assertIsNone(result['ask_owner'])
                self.assertIsNone(result['task'])

    def test_task_does_not_swallow_a_broken_escalation(self):
        # ask_owner 写坏仍然失败关闭（旧契约不变），task 只是额外挂在成功的答复上。
        with self.assertRaises(ValueError):
            consumer_safety.finalize_reply(json.dumps({
                'answer': 'x', 'ask_owner': '要不要？', 'task': {'state': 'running'}}))


class FakeBridge:
    """最小假 bridge：记下 ``events reply`` 的 argv，用来钉住任务开关。"""

    def __init__(self):
        self.calls = []
        self.notifications = []
        self.cards = []
        self.card_error = False
        now = time.time()
        self.event = {'id': 7, 'kind': 'message', 'state': 'leased', 'consumer': 'dsh-main',
                      'claim_token': TOKEN, 'created_at': now, 'lease_until': now + 600,
                      'payload': {'message_id': 'om_x', 'text': '这三份 excel 汇总一下发群里',
                                  'chat_id': 'oc_group', 'sender_type': 'user',
                                  'task': {'id': 'tsk_ab12cd34ef56', 'title': '汇总 excel', 'step': 1}}}

    def reply_argv(self):
        return [argv for argv, _body in self.calls if argv[0] == 'reply']

    def __call__(self, argv, body=None):
        self.calls.append((list(argv), body))
        command = argv[0]
        if command == 'list':
            return {'events': [dict(self.event)]}
        if command == 'status':
            return {'external': {'reply_timeout_seconds': 600}}
        if command == 'reply':
            return {'ok': True}
        if command == 'notify':
            self.notifications.append((list(argv), body))
            return {'ok': True}
        if command == 'task':
            self.cards.append((list(argv), body))
            if self.card_error:
                raise RuntimeError('Bridge event command failed')
            return {'ok': True, 'task': {'id': 'tsk_card01', 'state': 'open'}}
        if command == 'fail':
            return {'ok': True}
        raise AssertionError('unexpected bridge command: %r' % (argv,))


class ProcessFlagsTests(unittest.TestCase):
    """运行时要带着台账更新去 reply，并且它本身不打扰本人。"""

    def run_process(self, agent_reply, bridge=None):
        bridge = bridge or FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: agent_reply,
                                     threading.Event())
        return bridge

    def test_task_update_is_passed_as_flags_on_the_reply_command(self):
        bridge = self.run_process(json.dumps({
            'answer': '读完 2/3，缺 3 月。',
            'task': {'id': 'tsk_ab12cd34ef56', 'state': 'running',
                     'note': '读完 2/3 个 excel，缺 3 月数据',
                     'artifacts': [{'path': '/tmp/out.xlsx', 'kind': 'file'}],
                     'open_question': '3 月的数据在哪个目录？'}}))
        argv = bridge.reply_argv()[0]
        self.assertEqual(argv[:6], ['reply', '--consumer', 'dsh-main', '--event', '7',
                                    '--claim-token'])
        self.assertEqual(argv[argv.index('--claim-token') + 1], TOKEN)
        self.assertEqual(argv[argv.index('--task-id') + 1], 'tsk_ab12cd34ef56')
        self.assertEqual(argv[argv.index('--task-state') + 1], 'running')
        self.assertEqual(argv[argv.index('--task-note') + 1], '读完 2/3 个 excel，缺 3 月数据')
        self.assertEqual(argv[argv.index('--task-question') + 1], '3 月的数据在哪个目录？')
        self.assertEqual(json.loads(argv[argv.index('--task-artifacts-json') + 1]),
                         [{'path': '/tmp/out.xlsx', 'kind': 'file'}])
        # 台账更新不是求助：不许因为它多一条私聊。
        self.assertEqual(bridge.notifications, [])

    def test_only_the_present_flags_are_emitted(self):
        bridge = self.run_process(json.dumps({'answer': '做完了', 'task': {'state': 'done'}}))
        argv = bridge.reply_argv()[0]
        self.assertEqual(argv[argv.index('--task-state') + 1], 'done')
        for absent in ('--task-id', '--task-note', '--task-question', '--task-artifacts-json'):
            self.assertNotIn(absent, argv)

    def test_a_reply_without_a_task_carries_no_task_flags(self):
        bridge = self.run_process(json.dumps({'answer': '直接答上了'}))
        argv = bridge.reply_argv()[0]
        self.assertEqual(argv, ['reply', '--consumer', 'dsh-main', '--event', '7',
                                '--claim-token', TOKEN])

    def test_the_update_is_journalled_and_replayed_identically(self):
        class LostResponse(RuntimeError):
            pass

        task = {'state': 'blocked', 'open_question': '3 月的数据在哪个目录？'}
        bridge = FakeBridge()
        submitted = []

        def generate(payload, event, stop):
            submitted.append('generated')
            return json.dumps({'answer': '等你的目录', 'task': task})

        def flaky(argv, body=None):
            if argv[0] == 'reply' and not submitted[1:]:
                submitted.append(list(argv))
                raise LostResponse('response lost after commit')
            return FakeBridge.__call__(bridge, argv, body)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with self.assertRaises(LostResponse):
                consumer_runtime.process(flaky, 'dsh-main', state, dict(bridge.event),
                                         generate, threading.Event())
            record = json.loads(consumer_runtime.attempt_path(state, bridge.event).read_text())
            self.assertEqual(record['task'], task)
            consumer_runtime.process(flaky, 'dsh-main', state, dict(bridge.event),
                                     generate, threading.Event())
        self.assertEqual(submitted[0], 'generated')
        self.assertEqual(len([item for item in submitted if item == 'generated']), 1)
        replays = bridge.reply_argv()
        self.assertEqual(len(replays), 1)
        self.assertEqual(replays[0][replays[0].index('--task-state') + 1], 'blocked')
        self.assertEqual(replays[0][replays[0].index('--task-question') + 1],
                         '3 月的数据在哪个目录？')


class CardDraftTests(unittest.TestCase):
    """``task_card`` 的接线：答复里的卡片 → 一条 ``task draft``，且只影响"请人批"这件事。

    PR-2 时这张卡只有 prompt 定义、没有任何代码消费；这一组用例钉的正是"接上了"。
    """

    CARD = {'summary': '【活】把社区同步群这周的消息汇总成一张表',
            'why_me': '只有我能读那个群',
            'steps': ['导出这周的消息', '按主题归类', '发回群里'],
            'touches': ['群消息'],
            'done_when': '群里能看到这张表'}

    def run_process(self, agent_reply, bridge=None):
        # 回执文件写在 attempt 旁边、生命周期跟着这条事件走：在目录还在时读出来，
        # 否则断言只能落在"没报错"上。
        bridge = bridge or FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: agent_reply,
                                     threading.Event())
            self.receipt = ''
            replies = bridge.reply_argv()
            if replies and '--receipt-file' in replies[0]:
                argv = replies[0]
                self.receipt = Path(argv[argv.index('--receipt-file') + 1]).read_text(
                    encoding='utf-8')
        return bridge

    def test_a_reply_with_a_card_drafts_it_for_approval(self):
        bridge = self.run_process(json.dumps({'answer': '这活得你点头，我先把卡发你。',
                                              'task_card': self.CARD}))
        self.assertEqual(len(bridge.cards), 1)
        argv, body = bridge.cards[0]
        self.assertEqual(argv[:2], ['task', 'draft'])
        self.assertEqual(argv[argv.index('--chat') + 1], 'oc_group')
        self.assertEqual(argv[argv.index('--origin-mid') + 1], 'om_x')
        self.assertEqual(argv[argv.index('--question-event') + 1], '7')
        self.assertEqual(json.loads(body)['steps'], self.CARD['steps'])
        # 正文与起草是两件事：回复照发（而且先发），起草只是附带
        self.assertEqual(len(bridge.reply_argv()), 1)

    def test_a_reply_without_a_card_drafts_nothing(self):
        bridge = self.run_process(json.dumps({'answer': '直接答上了'}))
        self.assertEqual(bridge.cards, [])

    def test_a_broken_card_is_dropped_and_said_out_loud(self):
        # 丢弃本身不能是静默的：这条消息的回执里必须说"卡片没起草"。
        bridge = self.run_process(json.dumps({'answer': '这活得你点头。',
                                              'task_card': {'summary': '只有一句话'}}))
        self.assertEqual(bridge.cards, [])
        self.assertIn('卡片没起草', self.receipt)

    def test_a_drafted_card_is_announced_in_the_receipt(self):
        self.run_process(json.dumps({'answer': '这活得你点头。', 'task_card': self.CARD}))
        self.assertIn('任务卡已发你私聊', self.receipt)

    def test_a_failed_draft_does_not_lose_the_answer(self):
        bridge = FakeBridge()
        bridge.card_error = True
        self.run_process(json.dumps({'answer': '这活得你点头。', 'task_card': self.CARD}),
                         bridge)
        self.assertEqual(len(bridge.reply_argv()), 1)
        self.assertEqual(len(bridge.cards), 1)


class PromptLedgerTests(unittest.TestCase):
    """prompt 里的台账段：有任务才出现，出现时讲清看到什么、怎么回填。"""

    def payload(self, **extra):
        payload = {'message_id': 'om_x', 'text': '继续', 'sender_type': 'user', 'context': []}
        payload.update(extra)
        return payload

    def tasks(self):
        return [{'id': 'tsk_ab12cd34ef56', 'title': '汇总 3 个 excel 并发群',
                 'state': 'running', 'updated_at': time.time(),
                 'steps': [{'n': 1, 'outcome': 'sent', 'note': '读完 1 月'},
                           {'n': 2, 'outcome': 'failed', 'note': '3 月文件打不开'}],
                 'artifacts': ['/tmp/out.xlsx'],
                 'open_question': '3 月的数据在哪个目录？'}]

    def test_ledger_is_rendered_when_tasks_are_present(self):
        prompt = build_prompt(self.payload(tasks=self.tasks()))
        self.assertIn('【任务台账】', prompt)
        self.assertIn('tsk_ab12cd34ef56', prompt)
        self.assertIn('汇总 3 个 excel 并发群', prompt)
        self.assertIn('状态：running', prompt)
        self.assertIn('读完 1 月', prompt)
        self.assertIn('3 月文件打不开', prompt)
        self.assertIn('/tmp/out.xlsx', prompt)
        self.assertIn('3 月的数据在哪个目录？', prompt)
        # 输出字段契约必须和消费者侧认可的键名/状态一致。
        for field in ('id', 'state', 'note', 'artifacts', 'open_question'):
            self.assertIn(field, prompt)
        for state in consumer_safety.TASK_STATES:
            self.assertIn(state, prompt)
        self.assertIn('只有确实还有下一步才写 running；交付完写 done；等本人拍板写 blocked '
                      '并在 open_question 里写出你要问什么', prompt)
        self.assertIn('如果这条消息不属于任何任务，不要输出 task 字段', prompt)
        self.assertIn('要回填台账时再加一个 task 对象', prompt)
        # 台账段之后仍然是"最后一行是 data JSON"的旧形状。
        self.assertEqual(json.loads(prompt[prompt.rindex('\n') + 1:])['message']['text'], '继续')

    def test_new_task_line_is_rendered(self):
        prompt = build_prompt(self.payload(
            task={'id': 'tsk_ab12cd34ef56', 'title': '汇总 excel', 'step': 1}))
        self.assertIn('这条消息刚起了一个任务', prompt)
        self.assertIn('当前是第 1 步', prompt)

    def test_no_ledger_block_without_tasks(self):
        for extra in ({}, {'tasks': []}, {'tasks': None}, {'tasks': 'broken'}, {'task': {}}):
            with self.subTest(extra=extra):
                prompt = build_prompt(self.payload(**extra))
                self.assertNotIn('【任务台账】', prompt)
                self.assertNotIn(TASK_PROTOCOL, prompt)
                self.assertNotIn('task 字段', prompt)

    def test_broken_ledger_entries_do_not_break_the_prompt(self):
        prompt = build_prompt(self.payload(tasks=['junk', {'id': 'tsk_1'}, {'title': '没有 id'},
                                                  {'id': 'tsk_2', 'steps': 'junk'}]))
        self.assertIn('tsk_1', prompt)
        self.assertIn('tsk_2', prompt)
        self.assertNotIn('没有 id', prompt)


if __name__ == '__main__':
    unittest.main()
