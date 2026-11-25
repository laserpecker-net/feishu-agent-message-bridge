"""教材日志：追加写、重放、幂等、上限、坏行可见、跨进程串行化。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge import teach


class ChannelReasonTests(unittest.TestCase):
    """默认开通之后，"没开"的每一种原因都要能说成人能照做的一句话。"""

    def test_waiting_too_long_means_the_account_has_no_self_chat_yet(self):
        from bridge import teach
        config = {'schema': 1, 'enabled': True, 'enabled_at_ms': 1}
        self.assertEqual(teach.channel_reason(config, teach.P2P_READ_SCOPE), 'detecting')
        self.assertEqual(teach.channel_reason(config, teach.P2P_READ_SCOPE, waited_s=10), 'detecting')
        self.assertEqual(
            teach.channel_reason(config, teach.P2P_READ_SCOPE,
                                 waited_s=teach.SELF_CHAT_DISCOVERY_WAIT_S + 1), 'no_self_chat')

    def test_explicit_disable_and_corruption_win_over_everything(self):
        from bridge import teach
        self.assertEqual(teach.channel_reason({'enabled': False}, waited_s=10 ** 6), 'disabled')
        self.assertEqual(teach.channel_reason({'corrupt': True}, waited_s=10 ** 6), 'corrupt')
        self.assertEqual(
            teach.channel_reason({'enabled': True, 'chat_id': 'oc_x'}, waited_s=10 ** 6), 'on')


class EntryNameTests(unittest.TestCase):
    """名称是给本人认的：记在「风格 · 汇报先给结论」，撤回也用这个名字。

    2026-11-06 本人原话："编号读不懂，有一个名称看起来直观一些。"
    """

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def test_the_model_can_name_an_entry_and_that_name_is_stored(self):
        result = teach.add_from_model(self.home, 'style', '以后汇报先给结论，再讲细节', 'om_1',
                                      name='汇报先给结论')
        self.assertEqual(result['entry']['name'], '汇报先给结论')
        (view,) = teach.state(self.home)['charter']
        self.assertEqual(view['name'], '汇报先给结论')

    def test_a_missing_or_bad_name_falls_back_to_the_owners_own_words(self):
        result = teach.add_from_model(self.home, 'style', '预算表在共享盘 finance 目录', 'om_1',
                                      name='')
        name = result['entry']['name']
        self.assertTrue(name)
        self.assertIn('预算表', name)
        self.assertLessEqual(len(name), teach.MAX_NAME_CHARS)

    def test_a_legacy_entry_without_a_name_still_gets_one_when_read(self):
        # 历史条目没有 name 字段：读的时候从正文取，不改写日志（历史不可变）。
        teach.append(self.home, teach.new_entry('principle', '引用文件时给绝对路径', 'om_1',
                                               1793790000000))
        (view,) = teach.state(self.home)['charter']
        self.assertEqual(view['name'], '引用文件时给绝对路径')

    def test_the_same_name_twice_gets_a_number_instead_of_failing_the_write(self):
        teach.add_from_model(self.home, 'style', '汇报先给结论', 'om_1', name='汇报先给结论')
        second = teach.add_from_model(self.home, 'style', '汇报先给数据', 'om_2', name='汇报先给结论')
        self.assertEqual(second['entry']['name'], '汇报先给结论（2）')

    def test_an_entry_can_be_looked_up_by_name_or_by_id(self):
        teach.add_from_model(self.home, 'style', '以后汇报先给结论', 'om_1', name='汇报先给结论')
        by_name = teach.find_entry(self.home, '汇报先给结论')
        by_id = teach.find_entry(self.home, 't-0001')
        self.assertEqual(by_name['id'], by_id['id'])
        self.assertEqual(teach.live_entry_id(self.home, '汇报先给结论'), 't-0001')

    def test_renaming_writes_a_record_and_leaves_the_text_alone(self):
        teach.add_from_model(self.home, 'principle', '不要称呼我为刘总，称呼我为宝哥', 'om_1')
        renamed = teach.rename(self.home, 't-0001', '称呼宝哥')
        self.assertEqual(renamed['name'], '称呼宝哥')
        self.assertEqual(renamed['text'], '不要称呼我为刘总，称呼我为宝哥')
        # 只追加一条 rename：旧记录还在，正文一个字没动（历史可回退）。
        lines = teach.log_path(self.home).read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('不要称呼我为刘总', lines[0])

    def test_renaming_can_target_the_current_name(self):
        teach.add_from_model(self.home, 'style', '以后汇报先给结论', 'om_1', name='汇报先给结论')
        renamed = teach.rename(self.home, '汇报先给结论', '汇报格式')
        self.assertEqual(renamed['id'], 't-0001')
        self.assertEqual(renamed['name'], '汇报格式')

    def test_renaming_to_an_existing_name_gets_a_number_instead_of_a_duplicate(self):
        teach.add_from_model(self.home, 'style', '汇报先给结论', 'om_1', name='汇报先给结论')
        teach.add_from_model(self.home, 'style', '汇报先给数据', 'om_2', name='汇报先给数据')
        renamed = teach.rename(self.home, 't-0002', '汇报先给结论')
        self.assertEqual(renamed['name'], '汇报先给结论（2）')

    def test_renaming_a_withdrawn_entry_reports_instead_of_writing(self):
        teach.add_from_model(self.home, 'style', '以后汇报先给结论', 'om_1')
        teach.disable(self.home, 't-0001')
        self.assertIsNone(teach.rename(self.home, 't-0001', '随便'))

    def test_a_withdrawn_entry_cannot_be_withdrawn_again_by_name(self):
        teach.add_from_model(self.home, 'style', '以后汇报先给结论', 'om_1', name='汇报先给结论')
        teach.disable(self.home, 't-0001')
        self.assertIsNone(teach.live_entry_id(self.home, '汇报先给结论'))


class NoIncantationTests(unittest.TestCase):
    """本人不该背任何暗号：面向前台的说法里不许出现"忘掉最后一条"这种旧协议口令。

    真机 2026-11-06：这套口令是协议信号时代的残留，删了协议之后仍留在提示语里，
    本人看到只会问"这句话是什么意思"。判据：源码里只允许出现在"不要出现"的说明里。
    """

    def test_user_facing_sources_do_not_teach_the_owner_a_fixed_phrase(self):
        root = Path(__file__).resolve().parent.parent
        for name in ('agent_prompt.py', 'consumer_runtime.py', 'consumer_safety.py',
                     'bridge/cli.py', 'bridge/teach.py'):
            with self.subTest(file=name):
                text = (root / name).read_text()
                self.assertNotIn('忘掉最后一条', text)
                self.assertNotIn('忘掉 t-', text)


class TeachLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def add(self, text='引用文件时给绝对路径', mid='om_1', kind='principle', at_ms=None, **extra):
        record = teach.new_entry(kind, text, mid, at_ms or 1793790000000, **extra)
        return teach.append(self.home, record)

    def test_append_then_state_replays_add_records(self):
        self.add(mid='om_1')
        self.add(text='叫我就说「你」，别用「您」', mid='om_2', kind='style')
        state = teach.state(self.home)
        self.assertEqual(state['rev'], 2)
        self.assertEqual([entry['text'] for entry in state['charter']],
                         ['引用文件时给绝对路径', '叫我就说「你」，别用「您」'])
        self.assertEqual(state['notes'], [])
        self.assertIsNone(state['error'])

    def test_disable_hides_entry_but_keeps_rev_monotonic(self):
        entry = self.add(mid='om_1')
        teach.disable(self.home, entry['id'])
        state = teach.state(self.home)
        self.assertEqual(state['charter'], [])
        self.assertEqual(state['rev'], 2)
        self.assertIsNone(state['error'])

    def test_duplicate_message_id_is_idempotent(self):
        first = self.add(mid='om_same')
        again = self.add(text='重复投递不该写两遍', mid='om_same')
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(len(teach.state(self.home)['charter']), 1)

    def test_bad_line_is_counted_and_reported_not_silently_skipped(self):
        self.add(mid='om_1')
        with teach.log_path(self.home).open('a') as handle:
            handle.write('{oops\n')
        state = teach.state(self.home)
        self.assertEqual(len(state['charter']), 1)
        self.assertEqual(state['bad_lines'], 1)
        self.assertIn('1 行读不出来', state['error'])

    def test_entry_over_max_item_chars_is_rejected_with_reason(self):
        with self.assertRaises(ValueError) as caught:
            teach.new_entry('principle', 'x' * (teach.MAX_ITEM_CHARS + 1), 'om_long', 1)
        self.assertIn(str(teach.MAX_ITEM_CHARS), str(caught.exception))
        # 绕过 new_entry 直接塞记录也必须被拒：上限不能只靠一个入口把关。
        with self.assertRaises(ValueError):
            teach.append(self.home, {'v': 1, 'op': 'add', 'kind': 'principle', 'expose': 'say',
                                     'text': 'x' * (teach.MAX_ITEM_CHARS + 1),
                                     'message_id': 'om_long2', 'at_ms': 1})
        self.assertFalse(teach.log_path(self.home).exists())

    def test_more_than_max_entries_requires_explicit_revert(self):
        for index in range(teach.MAX_ENTRIES):
            self.add(text='准则 %d' % index, mid='om_%d' % index)
        with self.assertRaises(ValueError) as caught:
            self.add(text='第 201 条', mid='om_over')
        self.assertIn('撤回 t-0005', str(caught.exception))

    def test_read_config_missing_is_disabled_and_corrupt_is_flagged(self):
        self.assertEqual(teach.read_config(self.home), {})
        self.assertIsNone(teach.enabled_chat(self.home))
        teach.config_path(self.home).write_text('{oops')
        self.assertTrue(teach.read_config(self.home)['corrupt'])
        self.assertIsNone(teach.enabled_chat(self.home))

    def test_enabled_chat_requires_p2p_chat_id_and_flag(self):
        config = {'enabled': True, 'chat_id': 'oc_self'}
        teach.config_path(self.home).write_text(json.dumps(config))
        self.assertEqual(teach.enabled_chat(self.home), 'oc_self')
        teach.config_path(self.home).write_text(json.dumps({'enabled': False, 'chat_id': 'oc_self'}))
        self.assertIsNone(teach.enabled_chat(self.home))
        teach.config_path(self.home).write_text(json.dumps({'enabled': True, 'chat_id': 'group_1'}))
        self.assertIsNone(teach.enabled_chat(self.home))

    def test_entries_exposes_status_and_confidence_for_every_record(self):
        # 教材真身只有一个读者（teach.entries），检索/索引都从这里拿，不各自重放一遍 jsonl。
        first = self.add(mid='om_1')
        self.add(text='预算上限在共享盘', mid='om_2', kind='pointer')
        teach.disable(self.home, first['id'])
        entries = teach.entries(self.home)
        self.assertEqual([(entry['id'], entry['status']) for entry in entries],
                         [(first['id'], 'disabled'), ('t-0002', 'active')])
        self.assertEqual(entries[1]['confidence'], 1.0)

class NoRoutingLeftTests(unittest.TestCase):
    """判读全部交给模型：别留"半套规则"（2026-11-06 本人第二次拍板）。

    他要的是"让模型判断是什么指令，而不是协议信号和规则"。
    """

    def test_the_routing_functions_are_gone_from_the_source(self):
        source = Path(teach.__file__).read_text()
        for gone in ('def classify(', 'def command_for(', 'def intent_for(', 'def is_tagged(',
                     'def parse_text(', 'def route_decision(', 'def set_pending(',
                     'def read_pending(', 'def clear_pending(', '_COMPLAINT_MARKERS',
                     '_TASK_MARKERS', '_QUESTION_MARKERS', '_IMPERATIVE_MARKERS',
                     '_ASK_PREFIXES', '_TASK_PREFIXES', 'TAG_PREFIXES', 'PENDING_TTL_MS'):
            self.assertNotIn(gone, source, gone)

    def test_only_the_write_path_and_the_ask_bookkeeping_remain(self):
        # 判读没了，但"不是语言判断"的两件事必须还在：模型判出教材后的写盘、求助的待答登记。
        for name in ('add_from_model', 'pending_questions', 'register_question',
                     'answer_question', 'forget_last', 'disable'):
            self.assertTrue(hasattr(teach, name), name)


class ModelTeachingTests(unittest.TestCase):
    """模型判出"这是教材"之后落盘：写盘规矩仍在这一层，模型只给 kind 与正文。"""

    def setUp(self):
        self.home_dir = tempfile.TemporaryDirectory()
        self.home = self.home_dir.name

    def tearDown(self):
        self.home_dir.cleanup()

    def test_a_model_verdict_lands_in_the_charter(self):
        result = teach.add_from_model(self.home, 'style', '称呼本人为「某哥」', 'om_1')
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['entry']['kind'], 'style')
        self.assertEqual([entry['text'] for entry in teach.state(self.home)['charter']],
                         ['称呼本人为「某哥」'])

    def test_the_same_message_is_never_recorded_twice(self):
        # 消费者重试/重放时按来源消息 id 幂等：同一句话不许因为重放变成两条教材。
        first = teach.add_from_model(self.home, 'principle', '引用文件给绝对路径', 'om_1')
        second = teach.add_from_model(self.home, 'principle', '引用文件给绝对路径', 'om_1')
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['entry']['id'], first['entry']['id'])
        self.assertEqual(len(teach.state(self.home)['charter']), 1)

    def test_the_write_rules_are_still_enforced_here(self):
        # 模型给的 kind 不在白名单、正文为空/过长、缺幂等键：一律拒绝，不写半条。
        with self.assertRaises(ValueError):
            teach.add_from_model(self.home, 'not-a-kind', '随便', 'om_1')
        with self.assertRaises(ValueError):
            teach.add_from_model(self.home, 'fact', '   ', 'om_1')
        with self.assertRaises(ValueError):
            teach.add_from_model(self.home, 'fact', 'x' * (teach.MAX_ITEM_CHARS + 1), 'om_1')
        with self.assertRaises(ValueError):
            teach.add_from_model(self.home, 'fact', '预算表在共享盘', '')
        self.assertEqual(teach.state(self.home)['charter'], [])

    def test_a_disabled_entry_can_be_taught_again(self):
        # 幂等键是"来源消息"，不是"正文"：撤回之后他换一句话重说，必须能重新记上。
        first = teach.add_from_model(self.home, 'principle', '引用文件给绝对路径', 'om_1')
        teach.disable(self.home, first['entry']['id'])
        again = teach.add_from_model(self.home, 'principle', '引用文件给绝对路径', 'om_2')
        self.assertFalse(again['duplicate'])
        self.assertEqual([entry['text'] for entry in teach.state(self.home)['charter']],
                         ['引用文件给绝对路径'])


class MentionPlaceholderTests(unittest.TestCase):
    """@ 占位符是消息结构，不是教材正文：写进去会以 "@_user_1" 出现在每次提示词里。"""

    def test_clean_text_strips_the_placeholder(self):
        # 占位符是消息结构，不是本人说的话；写和读各清一次（旧教材不用重教也干净）。
        self.assertEqual(teach.clean_text('@_user_1 原则：引用文件时给绝对路径'),
                         '原则：引用文件时给绝对路径')
        self.assertEqual(teach.clean_text('@_user_12  叫我就说「你」'), '叫我就说「你」')


class QuestionBookkeepingTests(unittest.TestCase):
    """求助登记与答复：靠 parent_id 对上号、同一问题只答一次、不猜。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_register_is_idempotent_per_event(self):
        first = teach.register_question(self.home, 12, '要不要按 A 做？', 'decision', 'om_dm1', 'oc_self')
        again = teach.register_question(self.home, 12, '要不要按 A 做？', 'decision', 'om_dm2', 'oc_self')
        self.assertEqual(again['dm_message_id'], 'om_dm1')
        self.assertEqual(len(teach.pending_questions(self.home)), 1)
        self.assertEqual(first['qid'], 'q12')

    def test_answer_resolves_and_is_idempotent(self):
        teach.register_question(self.home, 12, '要不要按 A 做？', 'decision', 'om_dm1', 'oc_self')
        self.assertEqual(teach.question_for_parent(self.home, 'om_dm1')['qid'], 'q12')
        self.assertIsNone(teach.question_for_parent(self.home, 'om_other'))
        first = teach.answer_question(self.home, 'q12', '按 A 做', 'om_a1')
        self.assertFalse(first['duplicate'])
        self.assertTrue(teach.answer_question(self.home, 'q12', '再答一次', 'om_a2')['duplicate'])
        self.assertEqual(teach.pending_questions(self.home), [])

    def test_an_unanswered_ask_expires_instead_of_waiting_forever(self):
        """求助必须会过期。2026-11-05 晚：两条求助（群里有人只发了个「14」「18」）挂了一整夜，
        第二天它们还是「待答」，私聊每句话都被回「我有 2 个问题在等你」。"""
        now = int(time.time() * 1000)
        teach.register_question(self.home, 12, '要不要按 A 做？', 'decision', 'om_dm1', 'oc_self',
                                at_ms=now - teach.ASK_TTL_MS - 1000)
        self.assertEqual(teach.pending_questions(self.home), [], '过期求助不该还算待答')
        self.assertIsNone(teach.question_for_parent(self.home, 'om_dm1'),
                          '过期求助的引用回复不该再被当成答复')
        # 未过期的一条仍然算待答
        teach.register_question(self.home, 13, '另一个问题？', 'decision', 'om_dm2', 'oc_self',
                                at_ms=now - 1000)
        self.assertEqual([record['qid'] for record in teach.pending_questions(self.home)], ['q13'])
        self.assertEqual(teach.question_for_parent(self.home, 'om_dm2')['qid'], 'q13')

    def test_the_expiry_can_be_measured_against_an_explicit_clock(self):
        # 判据必须可复跑：不睡 30 分钟，直接把 now 传进来。
        base = 1_700_000_000_000
        teach.register_question(self.home, 12, '要不要按 A 做？', 'decision', 'om_dm1', 'oc_self',
                                at_ms=base)
        self.assertEqual(len(teach.pending_questions(self.home, now_ms=base + 1000)), 1)
        self.assertEqual(teach.pending_questions(self.home, now_ms=base + teach.ASK_TTL_MS + 1), [])

    def test_bad_question_line_is_not_silently_dropped(self):
        teach.register_question(self.home, 12, '问', 'fact', 'om_dm1', 'oc_self')
        with teach.questions_path(self.home).open('a') as handle:
            handle.write('{oops\n')
        self.assertEqual(len(teach.pending_questions(self.home)), 1)


class TidyTests(unittest.TestCase):
    """机械归并：只删完全重复、只合并同一天相邻同类；一个字都不改写。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def add(self, text, mid, kind='principle', at_ms=1793790000000):
        return teach.append(self.home, teach.new_entry(kind, text, mid, at_ms))

    def test_tidy_drops_exact_duplicates_and_reports_counts(self):
        self.add('引用文件时给绝对路径', 'om_1')
        self.add('引用文件时给绝对路径', 'om_2')
        self.add('叫我就说「你」', 'om_3', kind='style')
        result = teach.tidy(self.home)
        self.assertEqual(result['before'], 3)
        self.assertEqual(result['dropped_duplicates'], 1)
        self.assertEqual(result['after'], 2)
        self.assertEqual(result['dry_run'], False)
        self.assertEqual(len(teach.state(self.home)['charter']), 2)

    def test_tidy_does_not_change_any_entry_text(self):
        texts = ['引用文件时给绝对路径', '预算表在共享盘', '叫我就说「你」']
        self.add(texts[0], 'om_1')
        self.add(texts[1], 'om_2', kind='pointer')
        self.add(texts[2], 'om_3', kind='style')
        teach.tidy(self.home)
        snapshot = teach.state(self.home)
        visible = '\n'.join(entry['text'] for entry in snapshot['charter'] + snapshot['notes'])
        for text in texts:
            self.assertIn(text, visible)

    def test_tidy_dry_run_writes_nothing(self):
        self.add('引用文件时给绝对路径', 'om_1')
        self.add('引用文件时给绝对路径', 'om_2')
        before = teach.log_path(self.home).read_text()
        result = teach.tidy(self.home, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['dropped_duplicates'], 1)
        self.assertEqual(teach.log_path(self.home).read_text(), before)
        self.assertEqual(len(teach.state(self.home)['charter']), 2)

    def test_tidy_is_reversible_by_reading_the_history(self):
        first = self.add('引用文件时给绝对路径', 'om_1')
        second = self.add('叫我就说「你」', 'om_2')
        rev_before = teach.state(self.home)['rev']
        teach.tidy(self.home)
        self.assertGreater(teach.state(self.home)['rev'], rev_before)
        self.assertEqual(len(teach.state(self.home)['charter']), 1)
        # 合并后的新条目带着来源 id，原条目虽然不再生效但完整可查（history 就是回退依据）
        merged = [entry for entry in teach.state(self.home)['charter']][0]
        self.assertEqual(merged['ref'], {'from': [first['id'], second['id']]})
        self.assertIn('引用文件时给绝对路径', merged['text'])
        self.assertIn('叫我就说「你」', merged['text'])
        for original in (first['id'], second['id']):
            history = teach.find_entry(self.home, original)
            self.assertTrue(history['superseded'])
            self.assertTrue(history['text'])

    def test_tidy_does_not_merge_across_kinds_or_days(self):
        self.add('原则一', 'om_1', kind='principle', at_ms=1793790000000)
        self.add('资料一', 'om_2', kind='pointer', at_ms=1793790000000)
        self.add('原则二', 'om_3', kind='principle', at_ms=1793790000000 + 3 * 86400000)
        result = teach.tidy(self.home)
        self.assertEqual(result['merged'], [])
        self.assertEqual(len(teach.state(self.home)['charter']), 2)

    def test_tidy_keeps_superseded_entries_findable(self):
        kept = self.add('引用文件时给绝对路径', 'om_1')
        dropped = self.add('引用文件时给绝对路径', 'om_2')
        teach.tidy(self.home)
        self.assertFalse(teach.find_entry(self.home, kept['id'])['superseded'])
        history = teach.find_entry(self.home, dropped['id'])
        self.assertEqual(history['text'], '引用文件时给绝对路径')
        self.assertTrue(history['superseded'])


class AlreadyHandledTests(unittest.TestCase):
    """回放窗口时不能重复回执：教材/答复/候选都算处理过。"""

    def test_known_teaching_message_counts_as_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            teach.append(home, teach.new_entry('principle', '示例', 'om_1', 1000))
            self.assertTrue(teach.already_handled(home, 'om_1'))
            self.assertFalse(teach.already_handled(home, 'om_2'))

    def test_answer_message_counts_as_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            record = teach.register_question(home, '7', '要不要按 A 做？', 'decision',
                                             'om_dm', 'oc_self')
            teach.answer_question(home, record['qid'], '按 A 做', 'om_a')
            self.assertTrue(teach.already_handled(home, 'om_a'))

    def test_unknown_message_is_not_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(teach.already_handled(Path(directory), 'om_nope'))
