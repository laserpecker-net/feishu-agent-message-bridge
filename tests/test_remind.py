import time
import unittest

from bridge import remind

#: 固定一个"现在"，让判据不受跑测试的时刻影响。
NOW_MS = int(time.mktime((2026, 11, 6, 12, 0, 0, 0, 1, -1)) * 1000)


class ParsingTests(unittest.TestCase):
    def test_an_absolute_time_is_read_in_local_time(self):
        self.assertEqual(remind.format_when(remind.to_ms('2026-11-06 21:40', NOW_MS)),
                         '2026-11-06 21:40')

    def test_iso_style_with_t_and_seconds_is_accepted(self):
        self.assertEqual(remind.format_when(remind.to_ms('2026-11-06T21:40:30', NOW_MS)),
                         '2026-11-06 21:40')

    def test_a_bare_hour_means_the_next_time_it_comes_round(self):
        # 「晚上九点四十提醒我」不该被理解成一个已经过去的点。
        self.assertEqual(remind.format_when(remind.to_ms('21:40', NOW_MS)), '2026-11-06 21:40')
        self.assertEqual(remind.format_when(remind.to_ms('09:00', NOW_MS)), '2026-11-07 09:00')

    def test_a_date_written_as_one_number_is_not_taken_as_milliseconds(self):
        # `20261106214000` 看着像日期，当成毫秒就是 2612 年——会变成一条永远不响的提醒。
        for bad in ('20261106214000', '1793790000'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    remind.to_ms(bad)

    def test_epoch_milliseconds_pass_through(self):
        self.assertEqual(remind.to_ms(NOW_MS + 60000, NOW_MS), NOW_MS + 60000)

    def test_garbage_is_reported_instead_of_guessed(self):
        for bad in ('', '   ', '明天早上', '2026-13-40 99:99', '25:00'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    remind.to_ms(bad, NOW_MS)


class DescribeTests(unittest.TestCase):
    def test_relative_time_is_said_in_plain_words(self):
        self.assertEqual(remind.describe(NOW_MS + 40 * 1000, NOW_MS), '马上')
        self.assertEqual(remind.describe(NOW_MS + 9 * 60000, NOW_MS), '9 分钟后')
        self.assertEqual(remind.describe(NOW_MS + 3600000, NOW_MS), '1 小时后')
        self.assertEqual(remind.describe(NOW_MS + 90 * 60000, NOW_MS), '1 小时 30 分钟后')
        self.assertEqual(remind.describe(NOW_MS - 5 * 60000, NOW_MS), '已经过了 5 分钟')


class BodyTests(unittest.TestCase):
    def test_the_body_says_who_asked_and_when(self):
        body = remind.body_for({'text': '给张三打电话', 'created_at_ms': NOW_MS,
                                'fire_at_ms': NOW_MS + 3600000}, NOW_MS + 3600000)
        self.assertIn('⏰ 提醒你：给张三打电话', body)
        self.assertIn('（你 2026-11-06 12:00 让我这个点提醒你）', body)

    def test_a_late_reminder_says_it_is_late_instead_of_pretending(self):
        body = remind.body_for({'text': '给张三打电话', 'created_at_ms': NOW_MS,
                                'fire_at_ms': NOW_MS + 3600000}, NOW_MS + 3600000 + 12 * 60000)
        self.assertIn('让我这个点提醒你', body)      # 谁让它提醒的，别被"迟到"顶掉
        self.assertIn('原定 2026-11-06 13:00', body)
        self.assertIn('晚了 12 分钟', body)

    def test_the_body_never_at_anyone(self):
        # @ 人会把 bridge 自己的消息变成下一条待办（扫描只收 is_at_me），等于自己给自己派活。
        for now_ms in (NOW_MS, NOW_MS + 12 * 60000):
            self.assertNotIn('@', remind.body_for(
                {'text': '给张三打电话', 'created_at_ms': NOW_MS,
                 'fire_at_ms': NOW_MS + 3600000}, now_ms))

    def test_an_empty_body_still_produces_a_usable_message(self):
        body = remind.body_for({'text': '   ', 'created_at_ms': NOW_MS, 'fire_at_ms': NOW_MS},
                               NOW_MS)
        self.assertIn('（没写内容）', body)
