"""L3 感知延迟：一条消息的三段耗时（采集 / 消费者 / 投递）。

判据（设计稿 §3 T1）：
1. 分位数与手工计算一致（构造已知时间戳）；
2. `verified_at` 为空时用 `sent_at` 兜底；两者都空 → 这条样本不算"投递 0 秒"，只计数；
3. 负数区间（时钟回拨、采集时间早于消息时间）不进分位数，单独计数；
4. 空库给 `answered=0`，不抛异常；
5. `--window` 只影响样本量，不改字段名。

为什么这三段要分开：笼统的"它回得慢"没法验收。分开之后每一段都有自己的负责人和判据——
采集归轮询间隔（配置），消费者归模型单轮，投递归 sender。改一个地方只看一段的变化。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bridge import metrics
from bridge.store import Store


class LatencyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def put(self, mid, created_ms, received, generated, verified=None, sent=None, body='答'):
        """直接写两行时间戳：这里测的是时间数学，不是采集/投递管道本身。"""
        db = self.store.connection
        db.execute('INSERT INTO inbox(id,state,received_at,create_ms,text) VALUES (?,?,?,?,?)',
                   (mid, 'reply_queued' if verified is None else 'completed', received, created_ms, 'q'))
        db.execute('''INSERT INTO outbox(inbox_id,chat_id,seq,kind,uuid,body,body_hash,
                                        created_at,state,first_send_at,sent_at,verified_at)
                      VALUES (?,?,1,'reply',?,?,?,?,?,?,?,?)''',
                   (mid, 'oc_lat', 'u-' + mid, body, 'h', generated, 'reply_verified',
                    sent or generated, sent, verified))
        db.commit()

    def summary(self, window=50):
        return metrics.latency(self.home, window=window)


class PercentileTests(LatencyCase):
    def test_the_three_stages_add_up_and_match_hand_computation(self):
        # 采集 10s / 消费者 20s / 投递 5s，三条一模一样 → 分位数就是这些数
        for index in range(3):
            base = 1_000 + index * 100
            self.put('m%d' % index, int(base * 1000), received=base + 10, generated=base + 30,
                     sent=base + 34, verified=base + 35)
        result = self.summary()
        self.assertEqual(result['samples']['answered'], 3)
        self.assertEqual(result['stages']['collect']['p50'], 10.0)
        self.assertEqual(result['stages']['consumer']['p50'], 20.0)
        self.assertEqual(result['stages']['deliver']['p50'], 5.0)
        self.assertEqual(result['total']['p50'], 35.0)

    def test_percentiles_use_nearest_rank_and_never_interpolate(self):
        # 1..10 秒，nearest-rank p50 = 第 5 个 = 5，"p90" = 第 9 个 = 9
        for index, collect in enumerate(range(1, 11)):
            self.put('m%d' % index, 1_000_000 + index * 100_000,
                     received=1_000 + index * 100 + collect, generated=1_100 + index * 100,
                     sent=1_105 + index * 100, verified=1_105 + index * 100)
        stages = self.summary()['stages']['collect']
        self.assertEqual(stages['p50'], 5.0)
        self.assertEqual(stages['p90'], 9.0)
        self.assertEqual(stages['min'], 1.0)
        self.assertEqual(stages['max'], 10.0)


class MissingAndNegativeTests(LatencyCase):
    def test_without_verified_the_delivery_falls_back_to_sent_at(self):
        self.put('m1', 1_000_000, received=1_010, generated=1_020, sent=1_026)
        result = self.summary()
        self.assertEqual(result['stages']['deliver']['p50'], 6.0)
        self.assertEqual(result['samples']['deliver_missing'], 0)

    def test_a_reply_that_never_went_out_is_counted_not_treated_as_zero_seconds(self):
        # 生成之后就没发出去：算 0 秒会让"投递"看起来完美，正好把问题藏起来
        self.put('m1', 1_000_000, received=1_010, generated=1_020)
        result = self.summary()
        self.assertEqual(result['samples']['deliver_missing'], 1)
        self.assertEqual(result['stages']['deliver']['samples'], 0)  # 没有投递样本，不是 0 秒
        self.assertIsNone(result['stages']['deliver']['p50'])
        self.assertIsNone(result['total']['p50'])  # 三段不全就不算"合计" 

    def test_a_negative_interval_is_excluded_and_counted(self):
        # 时钟回拨：采集时间早于消息创建时间
        self.put('m1', 2_000_000, received=1_900, generated=1_920, sent=1_925, verified=1_926)
        result = self.summary()
        self.assertEqual(result['samples']['negatives'], 1)
        self.assertEqual(result['samples']['answered'], 0)
        self.assertIsNone(result['stages']['collect']['p50'])


class WindowAndEmptyTests(LatencyCase):
    def test_an_empty_home_reports_zero_instead_of_raising(self):
        result = self.summary()
        self.assertEqual(result['samples']['answered'], 0)
        self.assertIsNone(result['stages']['collect']['p50'])
        self.assertIsNone(result['total']['p50'])

    def test_the_window_only_changes_how_many_samples_are_used(self):
        for index, collect in enumerate([3, 9, 30]):
            self.put('m%d' % index, 1_000_000 + index * 100_000,
                     received=1_000 + index * 100 + collect, generated=1_100 + index * 100,
                     sent=1_105 + index * 100, verified=1_106 + index * 100)
        self.assertEqual(self.summary(window=1)['samples']['answered'], 1)
        self.assertEqual(self.summary(window=2)['samples']['answered'], 2)
        self.assertEqual(self.summary(window=50)['samples']['answered'], 3)
        # 窗口取最新两条（采集 30s 与 9s）：剩下两条的采集中位是 9
        self.assertEqual(self.summary(window=2)['stages']['collect']['p50'], 9.0)

    def test_the_scan_interval_comes_from_config_so_the_target_is_self_explaining(self):
        (self.home / 'config.json').write_text(json.dumps({'live_interval_seconds': 12}))
        self.assertEqual(self.summary()['scan_interval_seconds'], 12)
        (self.home / 'config.json').write_text(json.dumps({'live_interval_seconds': 3}))
        self.assertEqual(self.summary()['scan_interval_seconds'], 3)

    def test_queue_state_is_reported_so_silence_is_visible(self):
        # 有消息但一直没答：延迟分位数算不出来，但"积压"必须看得见
        self.store.connection.execute(
            'INSERT INTO inbox(id,state,received_at,create_ms,text) VALUES (?,?,?,?,?)',
            ('m_stuck', 'external_queued', 1_010, 1_000_000, 'q'))
        self.store.connection.commit()
        result = self.summary()
        self.assertEqual(result['samples']['answered'], 0)
        self.assertEqual(result['samples']['pending'], 1)


if __name__ == '__main__':
    unittest.main()
