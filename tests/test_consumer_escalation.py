"""答不上来时向本人私聊求助/提醒：consumer_runtime.process 的编排。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import threading
import time
import unittest

import consumer_runtime

TOKEN = 'a' * 32


class FakeBridge:
    """最小假 bridge：只实现 process() 会调的命令，并记录调用顺序。"""

    def __init__(self, *, state='leased', reply_ok=True, teach_ok=True, remind_ok=True):
        self.teach_ok = teach_ok
        self.remind_ok = remind_ok
        self.state = state
        self.reply_ok = reply_ok
        self.calls = []
        self.notifications = []
        now = time.time()
        self.event = {'id': 7, 'kind': 'message', 'state': state, 'consumer': 'dsh-main',
                      'claim_token': TOKEN, 'created_at': now, 'lease_until': now + 600,
                      'payload': {'message_id': 'om_x', 'text': '同事问：Meta 的 spend_cap 要不要调高？',
                                  'chat_id': 'oc_group', 'sender_type': 'user', 'context': [],
                                  'chat_type': 'group'}}

    def __call__(self, argv, body=None):
        self.calls.append((list(argv), body))
        command = argv[0]
        if command == 'list':
            return {'events': [dict(self.event, state=self.state)]}
        if command == 'status':
            return {'external': {'reply_timeout_seconds': 600}}
        if command == 'skip':
            self.skip_calls = getattr(self, 'skip_calls', [])
            self.skip_calls.append(list(argv))
            return {'ok': True, 'duplicate': False, 'message_id': 'om_x',
                    'reason': argv[argv.index('--reason') + 1]}
        if command == 'reply':
            # 回执走 --receipt-file，文件在临时 state 目录里，出 with 块就没了：调用当时读下来。
            if '--receipt-file' in argv:
                self.receipts = getattr(self, 'receipts', [])
                self.receipts.append(Path(argv[argv.index('--receipt-file') + 1]).read_text().strip())
            return {'ok': self.reply_ok}
        if command == 'notify':
            self.notifications.append((argv, body))
            return {'ok': True, 'sent': True, 'message_id': 'om_dm'}
        if command == 'fail':
            return {'ok': True}
        if argv[0] == 'remind':
            self.remind_calls = getattr(self, 'remind_calls', [])
            self.remind_calls.append(list(argv))
            if argv[1] == 'add':
                if not self.remind_ok:
                    return {'ok': False, 'reason': '时间读不出来：明天早上'}
                return {'ok': True, 'id': 3, 'name': '打电话给张三',
                        'text': '给张三打电话', 'fire_at': '2026-11-06 13:00',
                        'when': '1 小时后'}
            if argv[1] == 'cancel':
                if not self.remind_ok:
                    return {'ok': False, 'reason': '没有这条还没发的提醒'}
                return {'ok': True, 'id': 3, 'name': '打电话给张三', 'text': '给张三打电话',
                        'fire_at': '2026-11-06 13:00'}
        if argv[0] == 'teach':
            if len(argv) > 1 and argv[1] == 'revert':
                self.revert_calls = getattr(self, 'revert_calls', [])
                self.revert_calls.append((list(argv), body))
                if not self.teach_ok:
                    return {'ok': False, 'reason': '撤回命令失败'}
                target = argv[2] if len(argv) > 2 else 't-0005'
                entry_id = 't-0005' if target == '--last' else target
                return {'ok': True, 'id': entry_id, 'kind': 'style', 'label': '风格',
                        'name': '汇报先给结论', 'text': '以后汇报先给结论'}
            self.teach_calls = getattr(self, 'teach_calls', [])
            self.teach_calls.append((list(argv), body))
            if not self.teach_ok:
                return {'ok': False, 'reason': '教材已满 20 条'}
            name = '称呼宝哥'
            for index, argv in enumerate(self.teach_calls):
                if '--name' in argv:
                    name = argv[argv.index('--name') + 1]
            return {'ok': True, 'id': 't-%04d' % len(self.teach_calls), 'kind': 'style',
                    'label': '风格', 'name': name, 'text': '称呼本人为「某哥」', 'duplicate': False}
        raise AssertionError('unexpected bridge command: %r' % (argv,))


class EscalationTests(unittest.TestCase):
    def run_process(self, agent_reply, bridge=None):
        bridge = bridge or FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            consumer_runtime.process(bridge, 'dsh-main', state, dict(bridge.event),
                                     lambda payload, event, stop: agent_reply, threading.Event())
        return bridge

    def test_group_message_can_be_answered_with_nothing(self):
        bridge = self.run_process(json.dumps({'skip': '对方只是回了一句哦哦'}))
        self.assertEqual([argv[0] for argv, _ in bridge.calls if argv[0] in ('reply', 'skip')], ['skip'])
        self.assertEqual(bridge.skip_calls[0][bridge.skip_calls[0].index('--reason') + 1],
                         '对方只是回了一句哦哦')
        self.assertFalse(any(argv[0] == 'reply' for argv, _ in bridge.calls))
        self.assertEqual(bridge.notifications, [])

    def test_private_chat_cannot_answer_with_nothing(self):
        bridge = FakeBridge()
        bridge.event['payload']['chat_type'] = 'p2p'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: json.dumps({'skip': '不想回'}),
                                     threading.Event())
        # 私聊里沉默等于失联：skip 被拒，按 AgentFailed 收尾，而不是假装成功
        self.assertEqual([argv[0] for argv, _ in bridge.calls if argv[0] in ('reply', 'skip')], [])
        self.assertTrue(any(argv[0] == 'fail' for argv, _ in bridge.calls))

    def test_ask_owner_is_delivered_privately_after_the_reply(self):
        bridge = self.run_process(json.dumps({
            'answer': '这条要你拍板：spend_cap 我没权限改。',
            'ask_owner': {'question': 'DE/CA/EU 三个 Meta 账户的 spend_cap 要不要从 $1 调高？',
                          'needs': 'decision'}}))
        orders = [argv[0] for argv, _ in bridge.calls]
        self.assertIn('reply', orders)
        self.assertIn('notify', orders)
        self.assertLess(orders.index('reply'), orders.index('notify'))
        argv, body = bridge.notifications[0]
        self.assertEqual(argv[argv.index('--dedupe-key') + 1], 'event:7')
        # 求助必须带上事件 id：本人引用回复后 service 靠它找回原事件、重放后续跑。
        self.assertEqual(argv[argv.index('--question-event') + 1], '7')
        self.assertEqual(argv[argv.index('--needs') + 1], 'decision')
        self.assertIn('DE/CA/EU 三个 Meta 账户', body)
        self.assertIn('缺决定', body)
        self.assertIn('同事问：Meta 的 spend_cap', body)
        self.assertIn('我先回的', body)
        # 答复只认「引用回复」：求助正文必须把这句话说清楚，否则本人会平白回一句没人认的话。
        self.assertIn('引用回复我这条', body)
        self.assertNotIn('直接回我一句就行', body)

    def test_plain_reply_does_not_pester_the_owner(self):
        bridge = self.run_process(json.dumps({'answer': '直接答上了'}))
        self.assertEqual(bridge.notifications, [])

    def test_adapter_failure_notifies_the_owner(self):
        def explode(payload, event, stop):
            raise RuntimeError('adapter crashed')

        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     explode, threading.Event())
        orders = [argv[0] for argv, _ in bridge.calls]
        self.assertIn('fail', orders)
        self.assertIn('notify', orders)
        argv, body = bridge.notifications[0]
        self.assertEqual(argv[argv.index('--dedupe-key') + 1], 'fail:7:AgentFailed')
        self.assertIn('没能答上', body)

    def test_timeout_notifies_instead_of_dying_silently(self):
        bridge = FakeBridge()
        bridge.event['created_at'] = time.time() - 400  # 窗口只剩 200s < 240s 预算
        bridge.event['lease_until'] = time.time() + 200
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: '{}', threading.Event())
        orders = [argv[0] for argv, _ in bridge.calls]
        self.assertIn('fail', orders)
        self.assertIn('notify', orders)
        self.assertEqual(bridge.notifications[0][0][-1], 'fail:7:InsufficientTime')

    def test_escalation_is_journalled_so_a_retry_does_not_repeat_it(self):
        bridge = self.run_process(json.dumps({
            'answer': '先这样', 'ask_owner': {'question': '要不要调 spend_cap？', 'needs': 'decision'}}))
        self.assertEqual(len(bridge.notifications), 1)


class ReminderRuntimeTests(unittest.TestCase):
    """提醒：模型只说"几点、提醒什么"，登记与回执由运行时办。"""

    @staticmethod
    def reply_body(bridge):
        _argv, body = next((list(a), b) for a, b in bridge.calls if a[0] == 'reply')
        receipts = getattr(bridge, 'receipts', None) or []
        return body + '\n\n' + receipts[-1] if receipts else body

    def run_process(self, agent_reply):
        bridge = FakeBridge()
        bridge.event['payload']['self_intent'] = 'infer'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: agent_reply, threading.Event())
        return bridge

    def test_a_reminder_is_registered_with_the_chat_and_source_message(self):
        bridge = self.run_process(json.dumps({
            'answer': '行，1 小时后我提醒你给张三打电话。',
            'remind': {'at': '2026-11-06 13:00', 'text': '给张三打电话', 'name': '打电话给张三'}}))
        argv, = bridge.remind_calls
        self.assertEqual(argv[:2], ['remind', 'add'])
        self.assertEqual(argv[argv.index('--at') + 1], '2026-11-06 13:00')
        self.assertEqual(argv[argv.index('--chat') + 1], 'oc_group')
        self.assertEqual(argv[argv.index('--message-id') + 1], 'om_x')
        self.assertEqual(argv[argv.index('--name') + 1], '打电话给张三')
        body = self.reply_body(bridge)
        self.assertIn('· 提醒设好了：2026-11-06 13:00（1 小时后）', body)
        self.assertIn('@ 我说「取消 打电话给张三」', body)

    def test_the_reminder_is_registered_before_the_reply(self):
        # 回执要写"设在哪一刻"，只有登记成功才知道；先回后设就只能在正文里承诺。
        bridge = self.run_process(json.dumps({
            'answer': '好。', 'remind': {'at': '2026-11-06 13:00', 'text': '给张三打电话'}}))
        orders = [argv[0] for argv, _ in bridge.calls]
        self.assertLess(orders.index('remind'), orders.index('reply'))

    def test_a_rejected_reminder_is_reported_instead_of_letting_the_reply_lie(self):
        bridge = FakeBridge(remind_ok=False)
        bridge.event['payload']['self_intent'] = 'infer'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: json.dumps({
                                         'answer': '行，明天早上提醒你。',
                                         'remind': {'at': '明天早上', 'text': '给张三打电话'}}),
                                     threading.Event())
        body = self.reply_body(bridge)
        self.assertIn('· 提醒没设上：时间读不出来：明天早上', body)
        self.assertNotIn('提醒设好了', body)

    def test_cancelling_a_reminder_runs_before_the_reply_and_reports_the_name(self):
        bridge = self.run_process(json.dumps({
            'answer': '那就不提醒了。', 'remind_cancel': '打电话给张三'}))
        argv, = bridge.remind_calls
        self.assertEqual(argv, ['remind', 'cancel', '打电话给张三'])
        body = self.reply_body(bridge)
        self.assertIn('· 提醒撤了：「打电话给张三」（原定 2026-11-06 13:00）', body)

    def test_a_failed_cancel_says_so(self):
        bridge = FakeBridge(remind_ok=False)
        bridge.event['payload']['self_intent'] = 'infer'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: json.dumps({
                                         'answer': '好。', 'remind_cancel': '打电话给张三'}),
                                     threading.Event())
        body = self.reply_body(bridge)
        self.assertIn('· 没撤掉：没有这条还没发的提醒', body)

    def test_a_malformed_remind_is_reported_as_not_set_up(self):
        bridge = self.run_process(json.dumps({'answer': '好。', 'remind': {'text': '给张三打电话'}}))
        self.assertFalse(getattr(bridge, 'remind_calls', []))
        self.assertIn('· 提醒没设上：时间或内容没看懂', self.reply_body(bridge))


class ModelTeachingTests(unittest.TestCase):
    """判读交给模型之后：模型判出"这是教材"，运行时负责落盘（`teach add`），回复照常发。"""

    @staticmethod
    def reply_body(bridge):
        """本人最终看到的正文部分（不含署名）：正文 + 换行 + 回执文件里的内容。

        回执不再拼进正文，而是走 ``--receipt-file`` 交给 bridge 排在署名后面，
        所以这里把两段按同样顺序接起来断言。
        """
        _argv, body = next((list(a), b) for a, b in bridge.calls if a[0] == 'reply')
        receipts = getattr(bridge, 'receipts', None) or []
        return body + '\n\n' + receipts[-1] if receipts else body

    @staticmethod
    def reply_argv(bridge):
        (argv, _body), = [(list(a), b) for a, b in bridge.calls if a[0] == 'reply']
        return argv

    def run_process(self, agent_reply, message_id='om_selfmsg'):
        bridge = FakeBridge()
        bridge.event['payload']['message_id'] = message_id
        bridge.event['payload']['self_intent'] = 'infer'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(bridge, 'dsh-main', Path(directory), dict(bridge.event),
                                     lambda payload, event, stop: agent_reply, threading.Event())
        return bridge

    def test_the_write_happens_before_the_reply_so_the_receipt_can_name_the_entry(self):
        # 回执要写"记在「风格」t-0001"，条目号只有写盘之后才知道 → 先写、后回，一条消息说清。
        bridge = self.run_process(json.dumps({
            'answer': '我理解成：以后称呼你「某哥」。以后每句话我都这么叫，别人问起也按这个叫。',
            'teach': {'kind': 'style', 'text': '称呼本人为「某哥」'}}))
        orders = [argv[0] for argv, _ in bridge.calls]
        self.assertLess(orders.index('teach'), orders.index('reply'))
        (argv, _body), = bridge.teach_calls
        self.assertEqual(argv[:2], ['teach', 'add'])
        self.assertEqual(argv[argv.index('--kind') + 1], 'style')
        self.assertEqual(argv[argv.index('--text') + 1], '称呼本人为「某哥」')
        # 幂等键必须是来源消息 id：同一句重放不会记两条教材。
        self.assertEqual(argv[argv.index('--message-id') + 1], 'om_selfmsg')
        # 回执四要素里的两项（记在哪、怎么撤）由 bridge 追加，模型正文不许自己编条目号。
        body = self.reply_body(bridge)
        self.assertIn('· 记在「风格」· 称呼宝哥', body)
        self.assertIn('说「撤回 称呼宝哥」', body)
        # 回执必须走 --receipt-file：bridge 要把它排在署名后面，落款不能夹在附注中间。
        argv = self.reply_argv(bridge)
        self.assertIn('--receipt-file', argv)
        (sent, _body), = [(list(a), b) for a, b in bridge.calls if a[0] == 'reply']
        self.assertNotIn('· 记在', _body)
        self.assertNotIn('忘掉', body)

    def test_a_bad_teach_field_does_not_block_the_reply(self):
        bridge = self.run_process(json.dumps({
            'answer': '我按"别太啰嗦"理解了。', 'teach': {'kind': 'not-a-kind', 'text': '随便'}}))
        self.assertFalse(getattr(bridge, 'teach_calls', []))
        self.assertIn('reply', [argv[0] for argv, _ in bridge.calls])
        # 丢字段不等于说了算：回执里必须明确"这句没生效"，不能静默。
        self.assertIn('没记上', self.reply_body(bridge))

    def test_a_failed_write_says_so_in_the_same_reply(self):
        # 真机踩过：写盘静默失败，而回复已经说了"记下了"。现在成败只由回执那一行说，
        # 所以失败时正文末尾是"没记上 + 原因"，没有第二套规则兜底、也不再补第二条私聊。
        bridge = FakeBridge(teach_ok=False)
        bridge.event['payload']['message_id'] = 'om_selfmsg'
        bridge.event['payload']['self_intent'] = 'infer'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(
                bridge, 'dsh-main', Path(directory), dict(bridge.event),
                lambda payload, event, stop: json.dumps({
                    'answer': '以后我先给结论，再讲细节。', 'teach': {'kind': 'style',
                                                                    'text': '以后先给结论'}}),
                threading.Event())
        body = self.reply_body(bridge)
        self.assertIn('没记上', body)
        self.assertIn('教材已满 20 条', body)
        self.assertIn('再发一遍', body)
        self.assertFalse(bridge.notifications)

    def test_a_revert_verdict_is_executed_by_the_runtime(self):
        # 真机 2026-11-06：模型自己跑 `feishu-bridge teach revert` 被沙箱拒（home 在工作区外），
        # 只能在私聊里向本人要权限。所以撤回和记一样，只报目标，执行交给运行时。
        bridge = self.run_process(json.dumps({
            'answer': '那条作废了，以后不再按它办。', 'teach_revert': 't-0005'}))
        (argv, _body), = bridge.revert_calls
        self.assertEqual(argv, ['teach', 'revert', 't-0005'])
        self.assertFalse(getattr(bridge, 'teach_calls', []))
        body = self.reply_body(bridge)
        self.assertIn('· 撤了「汇报先给结论」（风格：以后汇报先给结论）', body)

    def test_forget_the_last_one_maps_to_the_last_flag(self):
        bridge = self.run_process(json.dumps({'answer': '撤了刚才那条。', 'teach_revert': 'last'}))
        (argv, _body), = bridge.revert_calls
        self.assertEqual(argv, ['teach', 'revert', '--last'])
        # 本来说的是"刚才那条"，回执里必须给出真实的名称与内容，否则下次他想指认也找不到。
        self.assertIn('· 撤了「汇报先给结论」（风格：以后汇报先给结论）', self.reply_body(bridge))

    def test_a_bad_revert_target_is_dropped_instead_of_shelling_out(self):
        for bad in ('../../etc/passwd', 't-1', 't-0007; rm -rf /', 7, ['t-0007']):
            with self.subTest(bad=bad):
                bridge = self.run_process(json.dumps({'answer': '好', 'teach_revert': bad}))
                self.assertFalse(getattr(bridge, 'revert_calls', []))
                self.assertIn('reply', [argv[0] for argv, _ in bridge.calls])

    def test_a_failed_revert_says_so_in_the_same_reply(self):
        bridge = FakeBridge(teach_ok=False)
        bridge.event['payload']['self_intent'] = 'infer'
        with tempfile.TemporaryDirectory() as directory:
            consumer_runtime.process(
                bridge, 'dsh-main', Path(directory), dict(bridge.event),
                lambda payload, event, stop: json.dumps({
                    'answer': '那条不生效了。', 'teach_revert': 'last'}),
                threading.Event())
        body = self.reply_body(bridge)
        self.assertIn('没撤掉', body)
        self.assertIn('撤回命令失败', body)
        self.assertFalse(bridge.notifications)

    def test_a_revert_and_a_write_in_the_same_reply_run_revert_first(self):
        # "刚才说错了，改成 X" = 撤旧 + 记新。顺序反了会把刚记的那条撤掉。
        bridge = self.run_process(json.dumps({
            'answer': '改成：汇报 = 结论 + 数据，过程不讲。',
            'teach_revert': 't-0005',
            'teach': {'kind': 'style', 'text': '汇报 = 结论 + 数据，过程不讲'}}))
        calls = [argv for argv, _ in bridge.calls]
        revert_at = calls.index(['teach', 'revert', 't-0005'])
        add_at = next(index for index, argv in enumerate(calls) if argv[:2] == ['teach', 'add'])
        self.assertLess(revert_at, add_at)
        body = self.reply_body(bridge)
        self.assertIn('· 撤了「汇报先给结论」', body)
        self.assertIn('· 记在「风格」· 称呼宝哥', body)

    def test_a_message_from_the_teach_chat_never_escalates_by_private_message(self):
        # 私聊里这条回复本身就是给本人看的；再发一条私聊求助只是同一件事说两遍。
        bridge = self.run_process(json.dumps({
            'answer': '是我哪步不对——答得太啰嗦，还是没查就下结论？',
            'ask_owner': {'question': '他到底嫌我什么？', 'needs': 'decision'}}))
        self.assertFalse(bridge.notifications)

    def test_no_teach_field_means_no_extra_bridge_call(self):
        bridge = self.run_process(json.dumps({'answer': '好的'}))
        self.assertFalse(getattr(bridge, 'teach_calls', []))


if __name__ == '__main__':
    unittest.main()
