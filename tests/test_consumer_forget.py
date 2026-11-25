"""死行清理的正规出口：``feishu-bridge consumer forget``。

判据能直接重跑：``python3 -m pytest tests/test_consumer_forget.py -q``。
为什么不手改库：生产 `consumers` 表里躺着 `s1-demo` 与几个几个月前的死行，
手删没有审计、也没人知道删之前它长什么样；这个命令打印删了什么，活跃的还挡一道。
"""

import io
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from bridge import cli, consumerctl
from bridge.store import Store


class ForgetCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.now = 1_000_000.0
        self.store.register_consumer('dead-one', 'dsh', self.now - 90 * 86400)
        # 活行按**真实时钟**登记：CLI 那条用例不注入 now，走的是 time.time()。
        self.store.register_consumer('live-one', 'dsh', time.time() - 30)
        self.store.close()

    def tearDown(self):
        self.temp.cleanup()

    def names(self):
        store = Store(self.home / 'state.sqlite3')
        try:
            return [row['name'] for row in store.consumers()]
        finally:
            store.close()

    def test_dead_row_is_deleted_with_a_receipt(self):
        result = consumerctl.forget(self.home, 'dead-one')
        self.assertTrue(result['ok'])
        self.assertEqual(result['forgotten']['name'], 'dead-one')
        self.assertGreater(result['forgotten']['last_seen_age_seconds'], 86400)
        self.assertEqual(self.names(), ['live-one'])

    def test_live_row_is_refused_without_force(self):
        with self.assertRaises(PermissionError) as caught:
            consumerctl.forget(self.home, 'live-one')
        self.assertIn('--force', str(caught.exception))
        self.assertEqual(sorted(self.names()), ['dead-one', 'live-one'])

    def test_live_row_can_be_deleted_with_force(self):
        result = consumerctl.forget(self.home, 'live-one', force=True)
        self.assertTrue(result['ok'])
        self.assertEqual(self.names(), ['dead-one'])

    def test_unknown_name_is_an_error(self):
        with self.assertRaises(LookupError) as caught:
            consumerctl.forget(self.home, 'never-existed')
        self.assertIn('没有这个消费者', str(caught.exception))

    def test_cli_exit_codes(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(consumerctl.main(self.home, ['forget', 'dead-one']), 0)
        self.assertIn('dead-one', json.loads(buffer.getvalue())['forgotten']['name'])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(consumerctl.main(self.home, ['forget', 'live-one']), 1)
        self.assertFalse(json.loads(buffer.getvalue())['ok'])
        with self.assertRaises(SystemExit):
            consumerctl.main(self.home, ['forget', 'never-existed'])

    def test_cli_propagates_the_refusal_exit_code(self):
        """`feishu-bridge consumer forget` 拒绝时进程必须退出 1：脚本靠它分支，不能装成功。"""
        argv = ['feishu-bridge', '--home', str(self.home), 'consumer', 'forget', 'live-one']
        with unittest.mock.patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(str(self.home))
        self.assertEqual(caught.exception.code, 1)

    def test_no_subcommand_still_prints_status(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(consumerctl.main(self.home, []), 0)
        self.assertIn('consumers', json.loads(buffer.getvalue()))


if __name__ == '__main__':
    unittest.main()
