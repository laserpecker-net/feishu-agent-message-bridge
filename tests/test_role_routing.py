"""角色路由的判据（S3a PR-1）：**能领什么由角色决定，不看消费者名字**。

为什么必须有这一层：S3a 之前任何消费者都能领任何事件，回话端会抢到要动手的活，
240 秒后被租约砍断、记账记成"失败"——这类事故最容易出、也最难查。

规则只有一处：``bridge.store.ROLE_EVENT_KINDS``。这里钉住它，也钉住三个调用面
（Store 方法 / CLI / HTTP 网关）看到的是同一个答案。
"""

import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from bridge import cli, gateway
from bridge.store import ROLE_EVENT_KINDS, Store


class RoleRoutingCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / 'config.json').write_text(json.dumps({'app_daily_reply_cap': 20}))
        self.store = Store(self.home / 'state.sqlite3')
        self.now = 100.0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # -- 夹具 ---------------------------------------------------------------
    def emit(self, kind, key):
        payload = {'text': 'hello'} if kind == 'message' else {'question': '要我做吗'}
        if kind == 'work':
            payload = {'task_id': 'tsk_1', 'chat_id': 'oc_a'}
        self.assertTrue(self.store.emit_event(kind, '%s:%s' % (kind, key), payload, self.now))

    def claim(self, consumer, **kwargs):
        return self.store.claim_events(consumer, self.now, **kwargs)

    # -- 注册 ---------------------------------------------------------------
    def test_register_defaults_to_replier_and_accepts_worker(self):
        self.assertEqual(self.store.register_consumer('a', 'dsh', self.now)['role'], 'replier')
        self.assertEqual(self.store.register_consumer('b', 'dsh', self.now, role='worker')['role'],
                         'worker')

    def test_register_rejects_unknown_role(self):
        with self.assertRaises(ValueError) as caught:
            self.store.register_consumer('a', 'dsh', self.now, role='admin')
        self.assertIn('role', str(caught.exception))

    def test_reregister_switches_role_without_losing_history(self):
        self.store.register_consumer('a', 'dsh', self.now)
        self.emit('message', 'x')
        self.claim('a')                                        # claims +1
        switched = self.store.register_consumer('a', 'dsh', self.now + 1, role='worker')
        self.assertEqual(switched['role'], 'worker')
        self.assertEqual(switched['claims'], 1, '改行当干活端不该丢掉它领过的事件计数')

    # -- 谁能领什么 ---------------------------------------------------------
    def test_replier_claims_message_and_attention(self):
        self.store.register_consumer('replier-1', 'dsh', self.now)
        self.emit('message', 'm')
        self.emit('attention', 'a')
        claimed = self.claim('replier-1')
        self.assertEqual(sorted(event['kind'] for event in claimed), ['attention', 'message'])

    def test_replier_cannot_claim_work(self):
        self.store.register_consumer('replier-1', 'dsh', self.now)
        self.emit('work', 'w')
        with self.assertRaises(ValueError) as caught:
            self.claim('replier-1', kind='work')
        message = str(caught.exception)
        self.assertIn('worker', message)
        self.assertIn('replier', message)

    def test_worker_claims_work_only(self):
        self.store.register_consumer('worker-1', 'dsh', self.now, role='worker')
        self.emit('work', 'w')
        self.emit('message', 'm')
        claimed = self.claim('worker-1')
        self.assertEqual([event['kind'] for event in claimed], ['work'])

    def test_worker_cannot_claim_message(self):
        self.store.register_consumer('worker-1', 'dsh', self.now, role='worker')
        self.emit('message', 'm')
        with self.assertRaises(ValueError) as caught:
            self.claim('worker-1', kind='message')
        self.assertIn('replier', str(caught.exception))

    def test_work_events_are_not_claimed_by_repliers_even_without_kind(self):
        self.store.register_consumer('replier-1', 'dsh', self.now)
        self.store.register_consumer('worker-1', 'dsh', self.now, role='worker')
        self.emit('work', 'w')
        self.assertEqual(self.claim('replier-1'), [])
        self.assertEqual([event['kind'] for event in self.claim('worker-1')], ['work'])

    def test_role_table_is_the_single_source(self):
        self.assertEqual(ROLE_EVENT_KINDS['replier'], ('message', 'attention'))
        self.assertEqual(ROLE_EVENT_KINDS['worker'], ('work',))

    def test_exhausted_lease_reclaim_respects_roles(self):
        """回话端不该把干活端的事件判成"重试到头"（以前任何角色都会这么干）。"""
        self.store.register_consumer('replier-1', 'dsh', self.now)
        self.store.register_consumer('worker-1', 'dsh', self.now, role='worker')
        self.emit('work', 'w')
        claimed = self.claim('worker-1', lease_seconds=1, next_attempt_cap=1)
        self.assertEqual(len(claimed), 1)
        # 租约过期、尝试次数也已到顶：worker 再领会判死信，replier 领不到也不该动它
        self.assertEqual(self.claim('replier-1', next_attempt_cap=1), [])
        self.assertEqual(self.store.get_event(claimed[0]['id'])['state'], 'leased')

    # -- CLI ----------------------------------------------------------------
    def run_cli(self, argv):
        """命令行入口按真实姿态跑：``feishu-bridge --home <home> events ...``。"""
        full = ['feishu-bridge', '--home', str(self.home)] + argv
        with unittest.mock.patch.object(sys, 'argv', full), redirect_stdout(io.StringIO()):
            # 成功时 main 返回 None（进程退出码 0），把它归一成 0 才好断言。
            return cli.main(str(self.home)) or 0

    def test_cli_register_has_role_and_claim_passes_it_through(self):
        self.assertEqual(self.run_cli(['events', 'register', '--consumer', 'w1',
                                       '--kind', 'dsh', '--role', 'worker']), 0)
        consumer = [c for c in self.store.consumers() if c['name'] == 'w1'][0]
        self.assertEqual(consumer['role'], 'worker')

        self.assertEqual(self.run_cli(['events', 'register', '--consumer', 'r1',
                                       '--kind', 'dsh']), 0)
        self.assertEqual([c for c in self.store.consumers() if c['name'] == 'r1'][0]['role'],
                         'replier')

    def test_cli_claim_with_work_kind_from_replier_exits_nonzero(self):
        self.assertEqual(self.run_cli(['events', 'register', '--consumer', 'r1',
                                       '--kind', 'dsh']), 0)
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(['events', 'claim', '--consumer', 'r1', '--kind', 'work'])
        self.assertIn('worker', str(caught.exception))

    # -- 网关 ---------------------------------------------------------------
    def test_gateway_claim_rejects_kind_outside_role(self):
        self.store.register_consumer('r1', 'other', self.now)
        api = gateway.Gateway(self.store, self.home, 'b' * 48, clock=lambda: self.now)
        headers = {'Authorization': 'Bearer ' + 'b' * 48}
        status, body = api.dispatch('POST', '/v1/events/claim', headers,
                                    json.dumps({'consumer': 'r1', 'kind': 'work'}).encode())
        self.assertEqual(status, 400)
        self.assertIn('worker', json.dumps(body, ensure_ascii=False))

    def test_gateway_claim_accepts_worker_for_work(self):
        self.store.register_consumer('w1', 'other', self.now, role='worker')
        self.emit('work', 'gw')
        api = gateway.Gateway(self.store, self.home, 'b' * 48, clock=lambda: self.now)
        headers = {'Authorization': 'Bearer ' + 'b' * 48}
        status, body = api.dispatch('POST', '/v1/events/claim', headers,
                                    json.dumps({'consumer': 'w1', 'kind': 'work'}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['events'][0]['kind'], 'work')


if __name__ == '__main__':
    unittest.main()
