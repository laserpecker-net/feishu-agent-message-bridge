"""``/v1/ingest``：长连接 sidecar 把事件送进桥的入口。

三条硬判据（对应设计稿 §2）：
1. **只回环**——非同机来源一律 403（这条通道不该被外部访问）；
2. **鉴权**——复用网关 token，缺/错就 401；
3. **幂等**——同一个 message_id 重复投递只入队一次，且进的是**与 as_user 相同的下游**。
"""
import json
import pathlib
import tempfile
import unittest

from bridge.gateway import Gateway, GatewayError
from bridge.store import Store

TOKEN = 'a' * 40
AUTH = {'authorization': 'Bearer ' + TOKEN}
LOOP = '127.0.0.1'
MID = 'om_' + 'c' * 20


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(pathlib.Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.close)
        self.gateway = Gateway(self.store, self.tmp.name, TOKEN, clock=lambda: 1000.0)

    def post(self, payload, remote=LOOP, headers=None):
        return self.gateway.dispatch('POST', '/v1/ingest', dict(headers or AUTH),
                                     json.dumps(payload).encode('utf-8'), remote=remote)

    def test_event_payload_is_queued_once(self):
        event = {'event': {'message': {'message_id': MID, 'chat_id': 'oc_1'}}}
        status, body = self.post(event)
        self.assertEqual(status, 200)
        self.assertEqual(body['queued'], 1)
        status, body = self.post(event)  # 重投
        self.assertEqual(status, 200)
        self.assertEqual(body['duplicates'], 1)
        self.assertEqual(body['queued'], 0)

    def test_flat_message_id_and_batch_are_accepted(self):
        status, body = self.post({'message_id': MID})
        self.assertEqual((status, body['queued']), (200, 1))
        batch = {'message_ids': ['om_' + 'd' * 20, 'om_' + 'e' * 20]}
        status, body = self.post(batch)
        self.assertEqual((status, body['queued']), (200, 2))

    def test_non_loopback_is_forbidden(self):
        status, body = self.post({'message_id': MID}, remote='10.1.2.3')
        self.assertEqual(status, 403)
        self.assertEqual(body['error']['code'], 'forbidden')

    def test_missing_remote_is_forbidden(self):
        status, body = self.gateway.dispatch('POST', '/v1/ingest', dict(AUTH),
                                            json.dumps({'message_id': MID}).encode(), remote=None)
        self.assertEqual(status, 403)

    def test_bad_token_is_unauthorized(self):
        status, body = self.gateway.dispatch('POST', '/v1/ingest',
                                            {'authorization': 'Bearer ' + 'f' * 40},
                                            json.dumps({'message_id': MID}).encode(), remote=LOOP)
        self.assertEqual(status, 401)

    def test_payload_without_message_id_is_rejected(self):
        status, body = self.post({'hello': 'world'})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'bad_request')

    def test_ingest_status_route(self):
        status, body = self.gateway.dispatch('GET', '/v1/ingest/status', dict(AUTH), b'', remote=LOOP)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])

    def test_ingested_message_enters_the_same_downstream_as_search(self):
        # 入队后必须能被 details 领取——证明"推送"与"搜索"走的是同一条下游
        self.post({'message_id': MID})
        job = self.store.claim_message('details', 1000.0, 150)
        self.assertIsNotNone(job, 'ingest 进来的消息必须能被 details 领到')
        self.assertEqual(job.get('message_id') or job.get('id'), MID)


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(pathlib.Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.close)

    def test_only_om_ids_are_accepted(self):
        result = self.store.enqueue_messages(['om_' + 'a' * 20, 'not-an-id', None, ''], 1000.0)
        self.assertEqual(result['queued'], 1)

    def test_empty_batch_is_a_noop(self):
        self.assertEqual(self.store.enqueue_messages([], 1000.0), {'queued': 0, 'duplicates': 0})


if __name__ == '__main__':
    unittest.main()
