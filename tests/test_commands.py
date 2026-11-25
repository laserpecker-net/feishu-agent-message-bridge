"""精确命令表：判定是确定性字符串匹配，不是"猜主人的意思"。

2026-11-17 主人拍板：说明书里的每一句都是**命令**，必须一个字不差；
「停一下」「继续」这类口语说法不算命令——它们会被回一句"命令要精确"，但**不执行**。

这里钉住四件事：

1. :func:`bridge.commands.match` 只认表里的词（含前后空格、句末一个标点）；
2. 近似说法不执行，但也不静默（回引导语，且有节流）；
3. 服务端只在**本人 + 私聊**时执行，群里一律不执行；
4. 说明书里的命令词与 :data:`bridge.commands.COMMANDS` 逐字一致（防漂移）。
"""

from tests.identity_doubles import UserModeDouble
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from bridge import commands, help as help_manual, hold
from bridge.service import Service

OWNER = 'ou_owner'
SELF_CHAT = 'oc_selfchat'
DM_CHAT = 'oc_private_dm'
GROUP_CHAT = 'oc_a_group'


def message(text, *, chat=SELF_CHAT, sender=OWNER, sender_type='user', mid='om_cmd'):
    return {
        'message_id': mid, 'chat_id': chat, 'msg_type': 'text',
        'sender': {'id': sender, 'sender_type': sender_type},
        'mentions': [{'id': OWNER, 'name': '本人'}],
        'body': {'content': json.dumps({'text': text}, ensure_ascii=False)},
        'create_time': str(int(time.time() * 1000)), 'deleted': False,
    }


class FakeTransport(UserModeDouble):
    def __init__(self, payloads):
        self.payloads = payloads
        self.sent = []

    def context(self, message):
        return []

    def probe_group(self, chat):
        pass

    def inventory(self):
        return []

    def get_message(self, mid):
        return self.payloads[mid]

    def send_text(self, chat_id, body, dedupe_uuid=None):
        self.sent.append((chat_id, body, dedupe_uuid))
        return 'om_sent_%d' % len(self.sent)

    def reply(self, mid, body, dedupe_uuid=None):
        self.sent.append((mid, body, dedupe_uuid))
        return 'om_sent_%d' % len(self.sent)



FakeTransport.owner_id = OWNER

def build_service(*payloads):
    folder = tempfile.TemporaryDirectory()
    home = Path(folder.name)
    config = {'target_open_id': OWNER, 'sending_enabled': True, 'agent_timeout': 60,
              'cursor_binary': '/fake', 'lark_binary': '/fake', 'self_chat_ids': [SELF_CHAT]}
    (home / 'config.json').write_text(json.dumps(config))
    service = Service(home, config)
    service.transport = FakeTransport({item['message_id']: item for item in payloads})
    service.authenticated.set()
    service.store.set_meta('service_epoch_ms', int(time.time() * 1000) - 86400000)
    return folder, service


def deliver(service, items):
    """把若干消息喂给 details 线程，等它们落成终态，然后停线程。"""
    now = time.time()
    service.store.begin_scan('live', 0, 1)
    service.store.ingest_search_page('live', [item['message_id'] for item in items], None, now)
    thread = threading.Thread(target=service.details, daemon=True)
    thread.start()
    deadline = time.time() + 8
    try:
        while time.time() < deadline:
            states = [service.store.get_message(item['message_id']) for item in items]
            if all(state and state['state'] not in ('queued', 'details_started', None) for state in states):
                break
            time.sleep(0.05)
    finally:
        service.stop.set()
        thread.join(5)
        assert not thread.is_alive(), 'worker thread leaked: %s' % thread.name
    return [service.store.get_message(item['message_id']) for item in items]


def bodies(service, mid):
    """这条消息触发的回执（有的命令按会话发，挂载点不是 inbox 行，所以按 uuid 一起捞）。"""
    rows = service.store.connection.execute(
        'SELECT body FROM outbox WHERE inbox_id=? OR uuid LIKE ? ORDER BY row_id',
        (mid, 'cmd:%s:%%' % mid)).fetchall()
    return [row['body'] for row in rows]


class MatchTests(unittest.TestCase):
    def test_every_advertised_command_hits(self):
        for word in commands.ADVERTISED:
            if word.endswith('：'):
                continue
            with self.subTest(word=word):
                self.assertIsNotNone(commands.match(word))
                self.assertIsNotNone(commands.match('  ' + word + '  '))
                self.assertIsNotNone(commands.match(word + '。'))
                self.assertIsNotNone(commands.match('@_user_1 ' + word + '！'))

    def test_help_is_case_insensitive(self):
        self.assertEqual(commands.match('HELP')[0], 'help')

    def test_remember_needs_payload(self):
        self.assertEqual(commands.match('记住：汇报先给结论'), ('remember', '汇报先给结论'))
        self.assertEqual(commands.match('记住:汇报先给结论'), ('remember', '汇报先给结论'))
        self.assertIsNone(commands.match('记住：'))
        self.assertIsNone(commands.match('记住'))

    def test_hold_and_release_use_the_same_switch(self):
        self.assertEqual(commands.match('下班')[0], 'hold')
        self.assertEqual(commands.match('上班')[0], 'release')
        # 开关不再自带一份词表：控制通道与私聊命令共用这一个判定。
        self.assertFalse(hasattr(hold, 'command_for'))

    def test_near_miss_words_do_not_match(self):
        for word in ('停一下', '继续', '下班了', '上班了', '暂停', '恢复', '帮助',
                     '说明书', '怎么用', '取消', '撤回一下', '状态如何'):
            with self.subTest(word=word):
                self.assertIsNone(commands.match(word))
                self.assertTrue(commands.looks_like_command(word))

    def test_execute_with_and_without_number(self):
        self.assertEqual(commands.match('执行'), ('execute', None))
        self.assertEqual(commands.match('执行 2'), ('execute', '2'))
        self.assertEqual(commands.match('执行2'), ('execute', '2'))
        self.assertEqual(commands.match(' 执行 '), ('execute', None))
        self.assertEqual(commands.match('执行。'), ('execute', None))
        for word in ('执行了', '执行一下', '执行 2 张', '执行x', '执行 -1'):
            with self.subTest(word=word):
                self.assertIsNone(commands.match(word))

    def test_plain_sentence_is_not_a_command(self):
        self.assertIsNone(commands.match('帮我把那个表格看一下'))
        self.assertFalse(commands.looks_like_command('帮我把那个表格看一下'))

    def test_guide_names_the_precise_commands(self):
        for word in ('下班', '上班', '状态', '清单', '撤回', 'help'):
            self.assertIn(word, commands.guide())

    def test_manual_matches_the_table(self):
        body = help_manual.text()
        for word in commands.ADVERTISED:
            with self.subTest(word=word):
                self.assertIn(word, body)
        for word in commands.ADVERTISED:
            self.assertIn('- ' + word, body, word)


class ManualTests(unittest.TestCase):
    def test_every_command_has_what_when_example(self):
        lines = help_manual.text().splitlines()
        for word in commands.ADVERTISED:
            with self.subTest(word=word):
                start = lines.index('- ' + word)
                self.assertTrue(lines[start + 1].startswith('  做什么：'), lines[start + 1])
                self.assertTrue(lines[start + 2].startswith('  什么时候用：'), lines[start + 2])
                self.assertTrue(lines[start + 3].startswith('  示例：'), lines[start + 3])
                # 四行都非空：只说"做什么"不算，得真的写了什么时候用、给了例子
                self.assertGreater(len(lines[start + 2].strip()), len('什么时候用：'))
                self.assertGreater(len(lines[start + 3].strip()), len('示例：'))

    def test_manual_lists_commands_in_table_order(self):
        body = help_manual.text()
        positions = [body.index('- ' + word) for word in commands.ADVERTISED]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(body.count('- help'), 1)

    def test_manual_explains_numbering(self):
        body = help_manual.text()
        self.assertIn('执行 2', body)
        self.assertIn('编号', body)


class DeliveryTests(unittest.TestCase):
    def run_one(self, text, *, chat=SELF_CHAT, mid='om_cmd'):
        folder, service = build_service(message(text, chat=chat, mid=mid))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message(text, chat=chat, mid=mid)])
        return service, bodies(service, mid)

    def test_status_reports_running_and_pending(self):
        service, said = self.run_one('状态')
        self.assertEqual(len(said), 1)
        self.assertIn('正常接待', said[0])
        self.assertIn('等你批的卡', said[0])
        self.assertEqual(service.store.get_message('om_cmd')['error'], 'command:status')

    def test_hold_then_status_then_release(self):
        folder, service = build_service(message('下班', mid='om_down'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message('下班', mid='om_down')])
        self.assertTrue(hold.read_state(service.home).get('active'))
        self.assertIn('已下班', hold.read_state(service.home) and bodies(service, 'om_down')[0])
        self.assertIn('我已下班', service.status_line(time.time()))
        service.run_command('release', None, SELF_CHAT, 'om_up', time.time() + 1)
        self.assertFalse(hold.read_state(service.home).get('active'))
        self.assertIn('正常接待', service.status_line(time.time()))

    def test_remember_list_revert_roundtrip(self):
        folder, service = build_service(message('记住：汇报先给结论', mid='om_remember'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message('记住：汇报先给结论', mid='om_remember')])
        said = bodies(service, 'om_remember')
        self.assertEqual(len(said), 1)
        self.assertIn('记下了', said[0])
        self.assertIn('汇报先给结论', said[0])

        service.run_command('list', None, SELF_CHAT, 'om_list_1', time.time() + 1)
        self.assertIn('汇报先给结论', bodies(service, 'om_remember')[-1])

        service.run_command('revert', None, SELF_CHAT, 'om_revert', time.time() + 2)
        self.assertIn('已撤回', bodies(service, 'om_revert')[-1])
        service.run_command('list', None, SELF_CHAT, 'om_list_2', time.time() + 3)
        self.assertIn('没有生效的记忆条目', bodies(service, 'om_list_2')[-1])

    def test_near_miss_gets_a_guide_but_does_nothing(self):
        service, said = self.run_one('停一下')
        self.assertEqual(len(said), 1)
        self.assertIn('命令要精确', said[0])
        self.assertFalse(hold.read_state(service.home).get('active'))
        self.assertEqual(service.store.get_message('om_cmd')['error'], 'command_unclear')

    def test_guide_is_throttled_per_chat(self):
        folder, service = build_service(
            message('停一下', mid='om_a'), message('继续', mid='om_b'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message('停一下', mid='om_a'), message('继续', mid='om_b')])
        self.assertEqual(len(bodies(service, 'om_a')), 1)
        self.assertEqual(bodies(service, 'om_b'), [])

    def test_group_command_is_not_executed(self):
        folder, service = build_service(message('状态', chat=GROUP_CHAT, mid='om_group'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        service.store.register_groups([{'chat_id': GROUP_CHAT, 'name': 'g', 'present': True}], time.time())
        deliver(service, [message('状态', chat=GROUP_CHAT, mid='om_group')])
        self.assertEqual(bodies(service, 'om_group'), [])
        self.assertNotEqual(service.store.get_message('om_group')['error'], 'command:status')

    def test_someone_else_command_is_not_executed(self):
        folder, service = build_service(message('状态', sender='ou_other', mid='om_other'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message('状态', sender='ou_other', mid='om_other')])
        self.assertEqual(bodies(service, 'om_other'), [])

    def test_replay_does_not_reply_twice(self):
        folder, service = build_service(message('状态', chat=DM_CHAT, mid='om_dm'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message('状态', chat=DM_CHAT, mid='om_dm')])
        first = len(bodies(service, 'om_dm'))
        service.run_command('status', None, DM_CHAT, 'om_dm', time.time() + 1)
        self.assertEqual(len(bodies(service, 'om_dm')), first)  # uuid 相同，不重发

    def test_private_dm_without_mention_still_works(self):
        folder, service = build_service(message('状态', chat=DM_CHAT, mid='om_dm2'))
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        deliver(service, [message('状态', chat=DM_CHAT, mid='om_dm2')])
        self.assertEqual(len(bodies(service, 'om_dm2')), 1)


if __name__ == '__main__':
    unittest.main()

class ExecuteTests(unittest.TestCase):
    """``执行``：批准一张待批卡；编号与 ``状态`` 同源。"""

    def setup_service(self, *titles, text='执行', mid='om_cmd', chat=SELF_CHAT, sender=OWNER,
                      register_group=False):
        payload = message(text, mid=mid, chat=chat, sender=sender)
        folder, service = build_service(payload)
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        if register_group:
            service.store.register_groups([{'chat_id': chat, 'name': 'g', 'present': True}],
                                          time.time())
        now = time.time()
        ids = [service.store.draft_task(SELF_CHAT, title, now + index)['id']
               for index, title in enumerate(titles)]
        deliver(service, [payload])
        return service, ids

    def task_state(self, service, task_id):
        return [task for task in service.store.tasks() if task['id'] == task_id][0]

    def test_single_pending_is_approved_as_command(self):
        service, ids = self.setup_service('补今天的项目日志')
        task = self.task_state(service, ids[0])
        self.assertEqual(task['state'], 'running')
        self.assertEqual(task['approval_kind'], 'command')
        self.assertEqual(task['approval_mid'], 'om_cmd')
        self.assertEqual([item['text'] for item in task['hints']], ['执行'])
        said = bodies(service, 'om_cmd')
        self.assertTrue(any('已批准' in body for body in said), said)

    def test_two_pending_without_number_lists_them(self):
        service, ids = self.setup_service('第一件事', '第二件事')
        self.assertEqual(self.task_state(service, ids[0])['state'], 'open')
        self.assertEqual(self.task_state(service, ids[1])['state'], 'open')
        said = bodies(service, 'om_cmd')
        self.assertTrue(any('1. 第一件事' in body and '2. 第二件事' in body for body in said), said)

    def test_number_picks_that_card(self):
        service, ids = self.setup_service('第一件事', '第二件事', text='执行 2')
        self.assertEqual(self.task_state(service, ids[0])['state'], 'open')
        self.assertEqual(self.task_state(service, ids[1])['state'], 'running')
        self.assertEqual(self.task_state(service, ids[1])['approval_kind'], 'command')

    def test_out_of_range_does_nothing(self):
        service, ids = self.setup_service('唯一一件事', text='执行 9')
        self.assertEqual(self.task_state(service, ids[0])['state'], 'open')
        said = bodies(service, 'om_cmd')
        self.assertTrue(any('没有第 9 张' in body for body in said), said)

    def test_no_pending_says_so(self):
        service, _ = self.setup_service()
        said = bodies(service, 'om_cmd')
        self.assertTrue(any('没有等你批的卡' in body for body in said), said)

    def test_repeating_execute_is_idempotent(self):
        service, ids = self.setup_service('补今天的项目日志')
        now = time.time()
        service.run_command('execute', None, SELF_CHAT, 'om_cmd', now, text='执行')
        task = self.task_state(service, ids[0])
        self.assertEqual(task['state'], 'running')
        self.assertEqual(len(task['hints']), 1)

    def test_group_command_is_not_executed(self):
        service, ids = self.setup_service('补今天的项目日志', chat=GROUP_CHAT,
                                          register_group=True)
        self.assertEqual(self.task_state(service, ids[0])['state'], 'open')
        self.assertNotEqual(service.store.get_message('om_cmd')['error'], 'command:execute')

    def test_someone_else_command_is_not_executed(self):
        service, ids = self.setup_service('补今天的项目日志', sender='ou_someone')
        self.assertEqual(self.task_state(service, ids[0])['state'], 'open')
        self.assertNotEqual(service.store.get_message('om_cmd')['error'], 'command:execute')

    def test_status_lists_cards_with_numbers(self):
        payload = message('状态', mid='om_status')
        folder, service = build_service(payload)
        self.addCleanup(folder.cleanup)
        self.addCleanup(service.store.close)
        now = time.time()
        service.store.draft_task(SELF_CHAT, '第一件事', now)
        service.store.draft_task(SELF_CHAT, '第二件事', now + 1)
        deliver(service, [payload])
        said = bodies(service, 'om_status')
        self.assertTrue(any('1. 第一件事' in body and '2. 第二件事' in body for body in said), said)
