import json
import os
import tempfile
import unittest
from pathlib import Path

from bridge import gateway
from bridge.store import Store, reply_uuid


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / 'config.json').write_text(json.dumps({'app_daily_reply_cap': 20}))
        self.store = Store(self.home / 'state.sqlite3')
        self.store.register_consumer('win-main', 'other', 10)
        self.token = 'a' * 48
        self.now = 10.0
        self.gateway = gateway.Gateway(self.store, self.home, self.token, clock=lambda: self.now)
        self.auth = {'Authorization': 'Bearer ' + self.token}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # -- helpers ------------------------------------------------------------
    def call(self, method, path, payload=None, headers=None, raw=None):
        if raw is None:
            raw = b'' if payload is None else json.dumps(payload).encode('utf-8')
        return self.gateway.dispatch(method, path, headers if headers is not None else self.auth, raw)

    def external_message(self, mid='m1', chat_type=None, sender_id='s1'):
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', [mid], None, self.now)
        job = self.store.claim_message('details', self.now, 10)
        payload = {'message_id': mid, 'chat_id': 'g1', 'sender_id': sender_id, 'text': 'hello'}
        if chat_type:
            payload['chat_type'] = chat_type
        self.assertTrue(self.store.complete_details(
            mid, job['attempt_id'], self.now + 1,
            {'version': 'v1', 'chat_id': 'g1', 'sender_id': sender_id, 'sender_type': 'user',
             'raw_json': '{}', 'text': 'hello'},
            state='external_queued',
            event={'kind': 'message', 'dedupe_key': 'message:' + mid, 'payload': payload}))

    def claim(self, kind=None):
        payload = {'consumer': 'win-main', 'limit': 5, 'lease_seconds': 600}
        if kind:
            payload['kind'] = kind
        status, body = self.call('POST', '/v1/events/claim', payload)
        self.assertEqual(status, 200, body)
        return body['events']

    # -- 任务账本（PR-3：远端与 CLI 同一批 store 方法）-------------------------
    def draft(self, chat='g1', origin='m1', title='把周报汇总成一张表'):
        return self.store.draft_task(chat, title, self.now, origin_mid=origin, message_id=origin)

    def test_tasks_list_shows_only_cards_waiting_for_approval(self):
        task = self.draft()
        self.store.approve_task(task['id'], approval_mid='om_a', approval_kind='quoted',
                                hints_text='做', now=self.now + 1)
        self.draft(origin='m2', title='第二件事')
        status, body = self.call('GET', '/v1/tasks')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['pending_approval'], 1)
        self.assertEqual(body['tasks'][0]['title'], '第二件事')

    def test_gateway_approve_matches_the_cli_state_machine(self):
        """G1：网关与 CLI 调同一个 store 方法——同一份账、同一组前置条件、同一个结果。"""
        task = self.draft()
        status, body = self.call('POST', '/v1/tasks/%s/approve' % task['id'],
                                 {'mid': 'om_win', 'kind': 'quoted', 'body': '做，但只做①'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['applied'])
        after = self.store.get_task(task['id'])
        self.assertEqual(after['state'], 'running')
        self.assertEqual(after['approval_kind'], 'quoted')
        self.assertEqual(after['approval_mid'], 'om_win')
        self.assertEqual(after['hints'][0]['text'], '做，但只做①')
        # 重放同一条 mid：幂等，不长出第二份 hints
        status, body = self.call('POST', '/v1/tasks/%s/approve' % task['id'],
                                 {'mid': 'om_win', 'kind': 'quoted', 'body': '做，但只做①'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['duplicate'])
        self.assertEqual(len(self.store.get_task(task['id'])['hints']), 1)

    def test_gateway_reject_and_note(self):
        task = self.draft()
        status, body = self.call('POST', '/v1/tasks/%s/note' % task['id'], {'body': '先问清楚再动'})
        self.assertEqual(status, 200, body)
        status, body = self.call('POST', '/v1/tasks/%s/reject' % task['id'],
                                 {'mid': 'om_no', 'body': '不做'})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.get_task(task['id'])['state'], 'cancelled')

    def test_gateway_rejects_an_unknown_task_with_404(self):
        """G2：不存在的任务给 404，而不是"操作成功"（静默成功比报错更难查）。"""
        status, body = self.call('POST', '/v1/tasks/tsk_doesnotexist/approve',
                                 {'mid': 'om_x', 'kind': 'quoted', 'body': '做'})
        self.assertEqual(status, 404, body)
        status, body = self.call('GET', '/v1/tasks/tsk_doesnotexist')
        self.assertEqual(status, 404, body)

    def test_gateway_refuses_an_unknown_approval_kind(self):
        task = self.draft()
        status, body = self.call('POST', '/v1/tasks/%s/approve' % task['id'],
                                 {'mid': 'om_x', 'kind': 'vibes', 'body': '做'})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.store.get_task(task['id'])['state'], 'open')

    # -- tests --------------------------------------------------------------
    def test_health_is_public_but_data_requires_token(self):
        status, body = self.call('GET', '/health', headers={})
        self.assertEqual((status, body['ok']), (200, True))
        for method, path in (('GET', '/v1/events/status'), ('GET', '/v1/events'),
                             ('POST', '/v1/events/claim')):
            status, body = self.call(method, path, payload={}, headers={})
            self.assertEqual(status, 401, body)
            self.assertEqual(body['error']['code'], 'unauthorized')
        status, body = self.call('GET', '/v1/events/status', headers={'X-Bridge-Token': self.token})
        self.assertEqual(status, 200, body)

    def test_claim_requires_registered_consumer(self):
        status, body = self.call('POST', '/v1/events/claim', {'consumer': 'ghost'})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'consumer_not_registered')

    def test_claim_then_reply_is_idempotent(self):
        self.external_message('m1')
        events = self.claim('message')
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event['payload']['message_id'], 'm1')
        self.assertTrue(event['claim_token'])
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'body': 'Windows 远程回复'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['duplicate'])
        self.assertEqual(body['outbox_state'], 'queued')
        outbox = self.store.get_outbox_by_inbox('m1')
        self.assertEqual(outbox['uuid'], reply_uuid('m1'))
        self.assertIn('Windows 远程回复', outbox['body'])
        # Same claim token + same body is an idempotent retry.
        status, again = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                  {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                   'body': 'Windows 远程回复'})
        self.assertEqual((status, again['duplicate']), (200, True), again)
        # GET event never exposes the claim token.
        status, view = self.call('GET', '/v1/events/%s' % event['id'])
        self.assertEqual(status, 200)
        self.assertNotIn('claim_token', view['event'])
        self.assertEqual(view['event']['state'], 'done')

    def test_reply_signature_renders_the_account_name(self):
        """网关是独立进程：署名里的 {owner} 必须从 status.json 的账号身份渲染。"""
        (self.home / 'status.json').write_text(json.dumps(
            {'identity': {'userName': '张三', 'openId': 'ou_x'}}))
        self.external_message('m-name')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'body': '远程回复正文'})
        self.assertEqual(status, 200, body)
        outbox = self.store.get_outbox_by_inbox('m-name')
        self.assertEqual(outbox['body'], '远程回复正文\n此回复信息来自张三的 AI 助手')

    def test_reply_signature_falls_back_to_neutral_without_identity(self):
        self.external_message('m-neutral')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'body': '远程回复正文'})
        self.assertEqual(status, 200, body)
        # 取不到账号身份：中性署名，绝不凭空造一个人名
        self.assertEqual(self.store.get_outbox_by_inbox('m-neutral')['body'],
                         '远程回复正文\n此回复信息来自账号本人的 AI 助手')

    def test_skip_closes_the_event_without_an_outbox(self):
        self.external_message('m-skip')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/skip' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'reason': '对方只是回了一句哦哦'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['duplicate'])
        self.assertIsNone(self.store.get_outbox_by_inbox('m-skip'))
        self.assertEqual(self.store.get_message('m-skip')['state'], 'ignored')
        # 远程消费者与 Mac 上的 CLI 走同一条实现：重放同一个 claim_token 幂等
        status, again = self.call('POST', '/v1/events/%s/skip' % event['id'],
                                  {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                   'reason': '对方只是回了一句哦哦'})
        self.assertEqual(status, 200, again)
        self.assertTrue(again['duplicate'])

    def test_skip_requires_a_reason(self):
        self.external_message('m-skip2')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/skip' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token']})
        self.assertEqual(status, 400, body)

    def test_group_reply_mentions_the_asker_not_the_owner(self):
        (self.home / 'config.json').write_text(json.dumps(
            {'app_daily_reply_cap': 20, 'target_open_id': 'ou_owner'}))
        self.gateway = gateway.Gateway(self.store, self.home, self.token, clock=lambda: self.now)
        self.external_message('m-ask', chat_type='group', sender_id='ou_peer')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'body': '<at user_id="ou_owner"></at> 收到'})
        self.assertEqual(status, 200, body)
        sent = self.store.get_outbox_by_inbox('m-ask')['body']
        self.assertTrue(sent.startswith('<at user_id="ou_peer"></at> '), sent)
        self.assertNotIn('ou_owner', sent)

    def test_reply_is_rejected_while_bridge_is_held(self):
        self.external_message('m-held')
        event = self.claim('message')[0]
        (self.home / 'hold.json').write_text(json.dumps({'active': True, 'since_ms': 1}))
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'], 'body': 'x'})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['error']['code'], 'held')
        self.assertIsNone(self.store.get_outbox_by_inbox('m-held'))
        # 恢复后同一个 claim 仍可正常回复
        (self.home / 'hold.json').write_text(json.dumps({'active': False}))
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'], 'body': '恢复后回复'})
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(self.store.get_outbox_by_inbox('m-held'))

    def test_reply_rejects_attention_event_and_bad_token(self):
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, self.now)
        event = self.claim('attention')[0]
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'], 'body': 'x'})
        self.assertEqual(status, 409, body)
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': 'deadbeef', 'body': 'x'})
        self.assertIn(status, (400, 409), body)
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token']})
        self.assertEqual(status, 400, body)
        # A claim token from another consumer is rejected.
        status, body = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'], 'body': ' '})
        self.assertEqual(status, 400, body)

    def test_attention_ack_and_message_ack_rejected(self):
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, self.now)
        attention = self.claim('attention')[0]
        status, body = self.call('POST', '/v1/events/%s/ack' % attention['id'],
                                 {'consumer': 'win-main', 'claim_token': attention['claim_token']})
        self.assertEqual(status, 200, body)
        self.external_message('m2')
        message = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/ack' % message['id'],
                                 {'consumer': 'win-main', 'claim_token': message['claim_token']})
        self.assertEqual(status, 409, body)

    def test_fail_retry_and_permanent(self):
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, self.now)
        event = self.claim('attention')[0]
        status, body = self.call('POST', '/v1/events/%s/fail' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'error': 'model timeout', 'retry_delay': 30})
        self.assertEqual((status, body['state']), (200, 'pending'), body)
        self.store.emit_event('attention', 'attention:b', {'message_id': 'm2'}, self.now)
        other = self.claim('attention')[0]
        self.assertNotEqual(other['id'], event['id'])
        status, body = self.call('POST', '/v1/events/%s/fail' % other['id'],
                                 {'consumer': 'win-main', 'claim_token': other['claim_token'],
                                  'error': 'cannot handle', 'permanent': True})
        self.assertEqual((status, body['state']), (200, 'dead'), body)

    def test_list_status_and_filters(self):
        self.store.emit_event('attention', 'attention:a', {'message_id': 'm1'}, self.now)
        status, body = self.call('GET', '/v1/events/status')
        self.assertEqual(status, 200, body)
        self.assertIn('external', body)
        self.assertIn('events', body)
        self.assertEqual([consumer['name'] for consumer in body['consumers']], ['win-main'])
        status, body = self.call('GET', '/v1/events?state=pending&limit=10')
        self.assertEqual((status, body['count']), (200, 1), body)
        self.assertNotIn('claim_token', body['events'][0])
        status, body = self.call('GET', '/v1/events?state=bogus')
        self.assertEqual(status, 400, body)

    def test_bad_requests_and_routes(self):
        status, body = self.call('GET', '/v1/nope')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))
        status, body = self.call('GET', '/v1/events')
        self.assertEqual(status, 200, body)
        status, body = self.call('POST', '/v1/events')
        self.assertEqual(status, 405, body)
        status, body = self.call('POST', '/v1/events/claim', raw=b'{not json')
        self.assertEqual((status, body['error']['code']), (400, 'bad_json'))
        status, body = self.call('POST', '/v1/events/claim', {'consumer': 'win-main', 'limit': 999})
        self.assertEqual(status, 400, body)
        status, body = self.call('POST', '/v1/events/claim', {'consumer': 'win-main', 'kind': 'bogus'})
        self.assertEqual(status, 400, body)
        status, body = self.call('POST', '/v1/events/claim', raw=b'x' * (gateway.MAX_BODY + 1))
        self.assertEqual((status, body['error']['code']), (413, 'payload_too_large'))
        status, body = self.call('GET', '/v1/events/999')
        self.assertEqual(status, 404, body)
        status, body = self.call('DELETE', '/v1/events/1')
        self.assertEqual(status, 405, body)

    def test_token_file_create_read_rotate(self):
        token, path, created = gateway.generate_token(self.home / 'sub')
        self.assertTrue(created)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(gateway.read_token(path), token)
        self.assertEqual(gateway.generate_token(self.home / 'sub')[0], token)
        rotated, _, created = gateway.generate_token(self.home / 'sub', rotate=True)
        self.assertTrue(created)
        self.assertNotEqual(rotated, token)
        path.write_text('not-hex\n')
        with self.assertRaises(RuntimeError):
            gateway.read_token(path)


    # -- say / retract（S1d：远程消费者与 CLI 同一条发布路径）---------------------
    def seed_sent(self, inbox_id, target_mid):
        self.store.connection.execute(
            'INSERT INTO outbox(inbox_id,chat_id,seq,kind,body,body_hash,target_mid,uuid,'
            'created_at,next_due,state,reply_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (inbox_id, 'g1', 9, 'say', '本桥发出去的', 'c' * 64, None, 'uuid-' + target_mid,
             self.now, self.now, 'sent', target_mid))
        self.store.connection.commit()

    def test_say_keeps_event_leased_and_is_idempotent(self):
        self.external_message('m1')
        event = self.claim('message')[0]
        payload = {'consumer': 'win-main', 'claim_token': event['claim_token'], 'body': '先看一眼'}
        status, body = self.call('POST', '/v1/events/%s/say' % event['id'], payload)
        self.assertEqual(status, 200, body)
        self.assertEqual((body['kind'], body['seq'], body['duplicate']), ('say', 1, False))
        row = self.store.connection.execute(
            "SELECT * FROM outbox WHERE kind='say'").fetchone()
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(self.store.get_message('m1')['state'], 'external_queued')
        # say 不结束事件：同一 claim_token 还能接着提交终回复
        self.assertEqual(self.store.get_event(event['id'])['state'], 'leased')
        status, again = self.call('POST', '/v1/events/%s/say' % event['id'], payload)
        self.assertEqual((status, again['duplicate']), (200, True), again)
        status, reply = self.call('POST', '/v1/events/%s/reply' % event['id'],
                                  {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                   'body': '做完了'})
        self.assertEqual(status, 200, reply)
        rows = self.store.connection.execute(
            'SELECT seq, kind FROM outbox ORDER BY seq').fetchall()
        self.assertEqual([(row['seq'], row['kind']) for row in rows], [(1, 'say'), (2, 'reply')])

    def test_retract_target_outside_bridge_is_404(self):
        self.external_message('m1')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/retract' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token'],
                                  'message_id': 'om_not_ours'})
        self.assertEqual(status, 404, body)
        self.assertEqual(body['error']['code'], 'not_found')

    def test_retract_queues_delete_and_refuses_second_retract(self):
        self.external_message('m1')
        self.seed_sent('m1', 'om_sent_by_bridge')
        event = self.claim('message')[0]
        payload = {'consumer': 'win-main', 'claim_token': event['claim_token'],
                   'message_id': 'om_sent_by_bridge'}
        status, body = self.call('POST', '/v1/events/%s/retract' % event['id'], payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(body['kind'], 'retract')
        row = self.store.connection.execute("SELECT * FROM outbox WHERE kind='retract'").fetchone()
        self.assertEqual(row['target_mid'], 'om_sent_by_bridge')
        # 同一个 uuid（默认派生）= 幂等重放；换 uuid 再来一次 = 冲突
        status, again = self.call('POST', '/v1/events/%s/retract' % event['id'], payload)
        self.assertEqual((status, again['duplicate']), (200, True), again)
        payload['uuid'] = 'another-retract-uuid'
        status, conflict = self.call('POST', '/v1/events/%s/retract' % event['id'], payload)
        self.assertEqual(status, 409, conflict)

    def test_unknown_event_action_is_404(self):
        self.external_message('m1')
        event = self.claim('message')[0]
        status, body = self.call('POST', '/v1/events/%s/shout' % event['id'],
                                 {'consumer': 'win-main', 'claim_token': event['claim_token']})
        self.assertEqual(status, 404, body)


if __name__ == '__main__':
    unittest.main()


class EventViewTeachingsTests(unittest.TestCase):
    """教材必须随每条 claim 注入，且两条 claim 路径（HTTP 网关 / CLI）给出逐字相同的 payload。"""

    def setUp(self):
        from bridge import teach as teach_log
        self.teach = teach_log
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / 'config.json').write_text(json.dumps({'app_daily_reply_cap': 20}))
        self.store = Store(self.home / 'state.sqlite3')
        self.store.register_consumer('win-main', 'other', 10)
        self.token = 'b' * 48
        self.now = 10.0
        self.gateway = gateway.Gateway(self.store, self.home, self.token, clock=lambda: self.now)
        self.auth = {'Authorization': 'Bearer ' + self.token}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def queue_event(self, mid='m1', payload=None, text='hello'):
        self.store.begin_scan('live', 1000, 2000)
        self.store.ingest_search_page('live', [mid], None, self.now)
        job = self.store.claim_message('details', self.now, 10)
        self.store.complete_details(
            mid, job['attempt_id'], self.now + 1,
            {'version': 'v1', 'chat_id': 'g1', 'sender_id': 's1', 'sender_type': 'user',
             'raw_json': '{}', 'text': 'hello'},
            state='external_queued',
            event={'kind': 'message', 'dedupe_key': 'message:' + mid,
                   'payload': payload if payload is not None else
                   {'message_id': mid, 'chat_id': 'g1', 'sender_id': 's1', 'text': text}})

    def enable(self):
        self.teach.config_path(self.home).write_text(json.dumps({'enabled': True, 'chat_id': 'oc_self'}))
        self.teach.append(self.home, self.teach.new_entry('principle', '引用文件给绝对路径', 'om_t1', 1))

    def test_claim_payload_carries_the_teaching_block(self):
        self.enable()
        self.queue_event()
        status, body = self.gateway.dispatch('POST', '/v1/events/claim', self.auth,
                                             json.dumps({'consumer': 'win-main',
                                                         'lease_seconds': 600}).encode())
        self.assertEqual(status, 200, body)
        teachings = body['events'][0]['payload']['teachings']
        self.assertEqual(teachings['rev'], 1)
        self.assertEqual([entry['text'] for entry in teachings['charter']], ['引用文件给绝对路径'])

    def test_claim_injects_only_the_notes_that_match_this_message(self):
        # L2-a：准则每轮必给，资料只给和这条消息相关的（两条 claim 路径同一套语义）。
        self.enable()
        self.teach.append(self.home, self.teach.new_entry(
            'pointer', '预算表在共享盘的 Finance/2026 目录', 'om_t2', 2))
        self.queue_event('om_unrelated', text='今天天气怎么样')
        self.queue_event('om_budget', text='预算表放在哪个目录？')
        status, body = self.gateway.dispatch('POST', '/v1/events/claim', self.auth,
                                             json.dumps({'consumer': 'win-main',
                                                         'lease_seconds': 600}).encode())
        self.assertEqual(status, 200, body)
        blocks = {event['payload']['message_id']: event['payload']['teachings']
                  for event in body['events']}
        self.assertEqual(blocks['om_unrelated']['notes'], [])
        self.assertEqual(blocks['om_unrelated']['charter'][0]['text'], '引用文件给绝对路径')
        self.assertEqual([entry['text'] for entry in blocks['om_budget']['notes']],
                         ['预算表在共享盘的 Finance/2026 目录'])

    def test_claim_payload_when_teaching_is_disabled_is_explicit(self):
        self.queue_event()
        status, body = self.gateway.dispatch('POST', '/v1/events/claim', self.auth,
                                             json.dumps({'consumer': 'win-main',
                                                         'lease_seconds': 600}).encode())
        self.assertEqual(status, 200, body)
        self.assertEqual(body['events'][0]['payload']['teachings'], {'enabled': False})

    def test_gateway_and_cli_claims_carry_identical_teachings(self):
        from contextlib import redirect_stdout
        import io
        from bridge import cli
        self.enable()
        self.queue_event()
        status, body = self.gateway.dispatch('POST', '/v1/events/claim', self.auth,
                                             json.dumps({'consumer': 'win-main',
                                                         'lease_seconds': 600}).encode())
        self.assertEqual(status, 200, body)
        gateway_block = body['events'][0]['payload']['teachings']
        self.store.fail_event('win-main', body['events'][0]['id'], self.now, 'retry later',
                              claim_token=body['events'][0]['claim_token'])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._events_claim(self.store, _claim_args('win-main'), self.home)
        cli_block = json.loads(buffer.getvalue())['events'][0]['payload']['teachings']
        self.assertEqual(gateway_block, cli_block)

    def test_view_fields_match_between_detailed_and_claim_shapes(self):
        from bridge.events_view import event_view
        self.queue_event()
        event = self.store.list_events(limit=1)[0]
        listing = event_view(event, detailed=True, with_token=True)
        listing.pop('claim_token')
        self.assertEqual(set(listing) - {'state', 'consumer', 'next_due', 'error'},
                         {'id', 'kind', 'dedupe_key', 'payload', 'created_at', 'attempts',
                          'lease_until'})

    def test_non_object_payload_fails_loudly(self):
        from bridge.events_view import event_view
        with self.assertRaises(ValueError):
            event_view({'id': 9, 'kind': 'message', 'dedupe_key': 'k', 'payload': '"plain string"',
                        'created_at': 1, 'state': 'pending', 'attempts': 0, 'consumer': None,
                        'lease_until': None, 'next_due': None, 'error': None},
                       teachings={'enabled': False})

def _claim_args(consumer):
    from bridge.cli import _events_parser
    return _events_parser().parse_args(['claim', '--consumer', consumer, '--lease', '600'])
