"""L2-a 记忆检索：准则必给、资料按相关性给、预算封顶、索引可删可重建。

设计稿 §5.7 的判据落在这里：200 条记忆时给定一条消息能命中预期条目；不再全量注入；
删掉 `memory.sqlite3` 后自动重建且条目数不变。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge import memory, teach


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / 'teach.json').write_text(json.dumps(
            {'schema': 1, 'enabled': True, 'chat_id': 'oc_self', 'max_entries': 200,
             'max_item_chars': 500, 'max_inject_chars': 4000}))

    def tearDown(self):
        self.temp.cleanup()

    def add(self, text, kind='principle', mid=None, at_ms=None):
        mid = mid or 'om_%d' % (len(teach.entries(self.home)) + 1)
        record = teach.new_entry(kind, text, mid, at_ms or (time.time() * 1000))
        return teach.append(self.home, record)


class TokenizerTests(unittest.TestCase):
    def test_chinese_is_split_into_bigrams_so_substring_queries_match(self):
        self.assertEqual(memory.tokens('本月收入合计'),
                         ['本月', '月收', '收入', '入合', '合计'])

    def test_ascii_words_stay_whole_and_lowercased(self):
        self.assertEqual(memory.tokens('SQLite FTS5 报告'), ['sqlite', 'fts5', '报告'])

    def test_junk_input_yields_no_tokens(self):
        self.assertEqual(memory.tokens(None), [])
        self.assertEqual(memory.tokens(''), [])
        self.assertEqual(memory.tokens('  ,。 '), [])


class RetrievalTests(MemoryCase):
    def test_principles_are_always_injected_even_without_a_matching_query(self):
        self.add('称呼我为宝哥', kind='principle')
        block = memory.retrieve(self.home, '今天几号')
        self.assertEqual([entry['text'] for entry in block['charter']], ['称呼我为宝哥'])
        self.assertEqual(block['notes'], [])
        self.assertEqual(block['used_ids'], ['t-0001'])
        self.assertTrue(block['enabled'])

    def test_notes_are_injected_only_when_the_message_matches_them(self):
        self.add('称呼我为宝哥', kind='principle')
        self.add('预算表在共享盘的 Finance/2026 目录', kind='pointer')
        hit = memory.retrieve(self.home, '预算表今年放在哪个目录？')
        self.assertEqual([entry['text'] for entry in hit['notes']],
                         ['预算表在共享盘的 Finance/2026 目录'])
        miss = memory.retrieve(self.home, '帮我把这句话翻译成英文')
        self.assertEqual(miss['notes'], [])
        self.assertIn('t-0001', miss['used_ids'])
        self.assertNotIn('t-0002', miss['used_ids'])

    def test_disabled_and_low_confidence_entries_are_not_injected(self):
        first = self.add('称呼我为宝哥', kind='principle')
        teach.disable(self.home, first['id'])
        # confidence 是 schema 2 的字段；旧行没有就按 1.0 算（见 teach.entries）。
        with teach.log_path(self.home).open('a') as handle:
            handle.write(json.dumps({'v': 2, 'op': 'add', 'id': 't-0009', 'kind': 'principle',
                                     'expose': 'say', 'text': '这条还没确认过', 'confidence': 0.2,
                                     'at_ms': int(time.time() * 1000), 'message_id': 'om_low'}) + '\n')
        block = memory.retrieve(self.home, '随便说说')
        self.assertEqual(block['charter'], [])
        self.assertEqual(block['used_ids'], [])

    def test_the_block_stays_within_the_prompt_budget(self):
        for index in range(40):
            self.add('第 %02d 条准则：' % index + '按绝对路径引用文件，不要用相对路径。' * 3,
                     kind='principle', mid='om_%02d' % index)
        block = memory.retrieve(self.home, '一句话', max_chars=600)
        used = sum(len(entry['text']) for entry in block['charter'] + block['notes'])
        self.assertLessEqual(used, 600)
        self.assertGreater(block['omitted'], 0)
        # 预算砍掉的是条目，不是"偷偷溢出"：被砍的条数如实报出来。
        self.assertEqual(block['omitted'], 40 - len(block['charter']))

    def test_two_hundred_entries_retrieve_the_expected_pointers(self):
        for index in range(198):  # 教材上限 200 条，留两个位置给下面两条真正要命中的
            kind = 'pointer' if index % 2 else 'fact'
            self.add('资料 %03d：关于主题%d 的说明' % (index, index), kind=kind,
                     mid='om_%03d' % index)
        self.add('预算口径以财务共享盘的 Finance/2026 为准', kind='pointer', mid='om_budget')
        self.add('发布流程走 release 分支，先跑全量测试', kind='pointer', mid='om_release')
        budget = memory.retrieve(self.home, '今年的预算口径以哪里为准？')
        texts = [entry['text'] for entry in budget['notes']]
        self.assertIn('预算口径以财务共享盘的 Finance/2026 为准', texts)
        self.assertNotIn('发布流程走 release 分支，先跑全量测试', texts)
        self.assertLessEqual(len(texts), memory.NOTE_LIMIT)

    def test_retrieval_does_not_rewrite_the_teaching_log(self):
        self.add('称呼我为宝哥', kind='principle')
        before = teach.log_path(self.home).read_bytes()
        memory.retrieve(self.home, '预算表在哪')
        memory.retrieve(self.home, '预算表在哪')
        self.assertEqual(teach.log_path(self.home).read_bytes(), before)

    def test_disabled_teaching_channel_returns_the_explicit_off_switch(self):
        (self.home / 'teach.json').write_text(json.dumps({'enabled': False, 'chat_id': 'oc_self'}))
        self.assertEqual(memory.retrieve(self.home, '随便'), {'enabled': False})


class IndexTests(MemoryCase):
    def test_the_index_is_a_derived_file_that_can_be_deleted_and_rebuilt(self):
        self.add('预算表在共享盘', kind='pointer')
        memory.retrieve(self.home, '预算表在哪')
        path = memory.cache_path(self.home)
        self.assertTrue(path.exists())
        report = memory.status(self.home)
        self.assertEqual((report['entries'], report['in_sync']), (1, True))
        path.unlink()
        rebuilt = memory.rebuild(self.home)
        self.assertEqual((rebuilt['entries'], rebuilt['in_sync']), (1, True))
        self.assertTrue(path.exists())
        self.assertEqual([entry['text'] for entry in memory.retrieve(self.home, '预算表在哪')['notes']],
                         ['预算表在共享盘'])

    def test_a_corrupt_index_is_rebuilt_instead_of_losing_memory(self):
        self.add('预算表在共享盘', kind='pointer')
        memory.retrieve(self.home, '预算表在哪')
        memory.cache_path(self.home).write_bytes(b'not a sqlite database')
        block = memory.retrieve(self.home, '预算表在哪')
        self.assertEqual([entry['text'] for entry in block['notes']], ['预算表在共享盘'])

    def test_a_home_with_only_principles_has_no_index_and_is_not_out_of_sync(self):
        # 真机现状：只有准则、没有资料 → 索引文件不存在是正常的，status 不该报不同步。
        self.add('称呼我为宝哥', kind='principle')
        memory.retrieve(self.home, '随便一句话')
        report = memory.status(self.home)
        self.assertEqual((report['present'], report['index_mode'], report['in_sync']),
                         (False, 'idle', True))

    def test_status_and_retrieval_agree_about_what_the_index_covers(self):
        # 撤回的条目不进索引；否则 status 和检索会对"索引是否同步"给出两种答案。
        first = self.add('称呼我为宝哥', kind='principle')
        self.add('预算表在共享盘', kind='pointer')
        teach.disable(self.home, first['id'])
        memory.retrieve(self.home, '预算表在哪')
        report = memory.status(self.home)
        self.assertEqual((report['entries'], report['records'], report['in_sync']), (1, 2, True))

    def test_a_stale_index_is_rebuilt_after_a_new_teaching(self):
        self.add('预算表在共享盘', kind='pointer')
        memory.retrieve(self.home, '预算表在哪')
        self.add('报销走 OA 系统里的财务审批', kind='pointer', mid='om_new')
        report = memory.status(self.home)
        self.assertFalse(report['in_sync'])
        block = memory.retrieve(self.home, '报销走哪个系统？')
        self.assertEqual([entry['text'] for entry in block['notes']],
                         ['报销走 OA 系统里的财务审批'])
        self.assertTrue(memory.status(self.home)['in_sync'])


class PromptBudgetTests(MemoryCase):
    def test_the_rendered_teaching_section_only_contains_retrieved_entries(self):
        from agent_prompt import teaching_section

        self.add('称呼我为宝哥', kind='principle')
        self.add('预算表在共享盘', kind='pointer')
        block = memory.retrieve(self.home, '今天天气怎么样')
        section = teaching_section(block)
        self.assertIn('称呼我为宝哥', section)
        self.assertNotIn('预算表在共享盘', section)
        self.assertLessEqual(len(section), 4000)


if __name__ == '__main__':
    unittest.main()
