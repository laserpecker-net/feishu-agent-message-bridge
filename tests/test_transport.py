import json
import subprocess
import time
import unittest
import threading
from bridge.transport import Transport, TransportError, mentioned, message_text, message_version, verify_reply
from unittest.mock import patch


def completed(stdout='', stderr='', returncode=0):
    return subprocess.CompletedProcess(['/fake/lark'], returncode, stdout, stderr)


class DirectMessageTests(unittest.TestCase):
    """`send_dm` 是私聊求助的唯一出口：按 open_id 发、带幂等 uuid、响应里带 chat_id。"""

    def transport(self, response):
        transport = Transport({}, threading.Event())
        captured = {}

        def api(method, path, params=None, data=None, search=False):
            captured.update({'method': method, 'path': path, 'params': params, 'data': data})
            return response
        transport.api = api
        return transport, captured

    def test_sends_to_the_open_id_and_returns_the_chat_id(self):
        transport, captured = self.transport({'message_id': 'om_1', 'chat_id': 'oc_dm'})
        result = transport.send_dm('ou_owner', '需要你确认', dedupe_key='event:7')
        self.assertEqual(result, {'message_id': 'om_1', 'chat_id': 'oc_dm'})
        self.assertEqual(captured['path'], '/open-apis/im/v1/messages')
        self.assertEqual(captured['params'], {'receive_id_type': 'open_id'})
        self.assertEqual(captured['data']['receive_id'], 'ou_owner')
        self.assertEqual(json.loads(captured['data']['content']), {'text': '需要你确认'})
        self.assertEqual(len(captured['data']['uuid']), 32)

    def test_same_dedupe_key_produces_the_same_request_uuid(self):
        first, captured_a = self.transport({'message_id': 'om_1', 'chat_id': 'oc_dm'})
        first.send_dm('ou_owner', 'A', dedupe_key='event:7')
        second, captured_b = self.transport({'message_id': 'om_1', 'chat_id': 'oc_dm'})
        second.send_dm('ou_owner', 'A', dedupe_key='event:7')
        self.assertEqual(captured_a['data']['uuid'], captured_b['data']['uuid'])

    def test_rejects_a_chat_id_where_an_open_id_belongs(self):
        transport, _ = self.transport({'message_id': 'om_1'})
        with self.assertRaises(TransportError):
            transport.send_dm('oc_not_a_user', 'x')

    def test_missing_message_id_fails_closed(self):
        transport, _ = self.transport({'chat_id': 'oc_dm'})
        with self.assertRaises(TransportError):
            transport.send_dm('ou_owner', 'x')


class RecallTests(unittest.TestCase):
    """撤回只有一条路：DELETE /open-apis/im/v1/messages/{mid}，错误交给既有分类。"""

    def transport(self, response=None, error=None):
        transport = Transport({}, threading.Event())
        captured = {}

        def api(method, path, params=None, data=None, search=False, before_request=None):
            captured.update({'method': method, 'path': path})
            if error is not None:
                raise error
            return response or {}
        transport.api = api
        return transport, captured

    def test_deletes_by_message_id(self):
        transport, captured = self.transport({})
        self.assertTrue(transport.delete_message('om_1'))
        self.assertEqual(captured['method'], 'DELETE')
        self.assertEqual(captured['path'], '/open-apis/im/v1/messages/om_1')

    def test_rejects_a_chat_id_where_a_message_id_belongs(self):
        transport, _ = self.transport({})
        with self.assertRaises(TransportError):
            transport.delete_message('oc_not_a_message')
        with self.assertRaises(TransportError):
            transport.delete_message(None)

    def test_permanent_error_propagates_for_the_outbox_to_park(self):
        transport, _ = self.transport(error=TransportError('Recall window expired', 'validation',
                                                           230050, False))
        with self.assertRaises(TransportError) as caught:
            transport.delete_message('om_1')
        self.assertFalse(caught.exception.retryable)


class SendTextIdempotencyKeyTests(unittest.TestCase):
    """主动 say 走 send_text：幂等键必须来自 outbox 行，重试不能变成刷屏。"""

    def transport(self):
        transport = Transport({}, threading.Event())
        captured = {}

        def api(method, path, params=None, data=None, search=False, before_request=None):
            captured.update(data or {})
            return {'message_id': 'om_say'}
        transport.api = api
        return transport, captured

    def test_caller_uuid_is_used_verbatim(self):
        transport, captured = self.transport()
        self.assertEqual(transport.send_text('oc_1', '我看看', dedupe_uuid='deadbeef'), 'om_say')
        self.assertEqual(captured['uuid'], 'deadbeef')

    def test_without_a_key_the_request_uuid_is_still_generated(self):
        transport, captured = self.transport()
        transport.send_text('oc_1', '挂起回执')
        self.assertEqual(len(captured['uuid']), 32)


class MessageTests(unittest.TestCase):
    def test_personal_id_not_name_or_all(self):
        self.assertTrue(mentioned({'mentions':[{'id':'ou_me'}]},'ou_me'))
        self.assertTrue(mentioned({'mentions':[{'id':{'open_id':'ou_me'}}]},'ou_me'))
        self.assertFalse(mentioned({'mentions':[{'id':'all','name':'张三'}]},'ou_me'))
        self.assertFalse(mentioned({'body':{'content':'@张三'}},'ou_me'))

    def test_post_and_text_are_readable(self):
        m={'msg_type':'post','body':{'content':json.dumps({'title':'问题','content':[[{'tag':'at','user_id':'ou_me'},{'tag':'text','text':'计算37×19'}]]})}}
        self.assertIn('计算37×19',message_text(m))
        self.assertIn('问题',message_text(m))
        self.assertEqual(message_text({'msg_type':'text','body':{'content':'{"text":"你好"}'}}),'你好')

    def test_source_version_changes_with_edit(self):
        self.assertNotEqual(message_version({'body':{'content':'one'}}),message_version({'body':{'content':'two'}}))

    def test_search_uses_server_accepted_whole_second_time_format(self):
        t=Transport({},threading.Event());captured={}
        def api(method,path,params,data,search):
            captured.update(data);return {'items':[],'has_more':False}
        t.api=api
        self.assertEqual(t.search(1792986423123,1792986723456),{'ids':[],'next_token':None,'notice':None})
        window=captured['filter']['time_range']
        self.assertNotIn('.',window['start_time'])
        self.assertNotIn('.',window['end_time'])

    def test_dispatch_guard_runs_after_rate_wait_and_before_process(self):
        t=Transport({'lark_binary':'/fake'},threading.Event());events=[]
        t._pace=lambda search=False:events.append('paced')
        def refuse():
            events.append('guard');raise RuntimeError('expired')
        with patch('bridge.transport.subprocess.run') as process:
            with self.assertRaises(RuntimeError):t.run(['api'],before_request=refuse)
            process.assert_not_called()
        self.assertEqual(events,['paced','guard'])

    def test_normal_reply_must_reference_exact_input(self):
        original={'message_id':'m','chat_id':'c'}
        reply={'chat_id':'c','parent_id':'m','sender':{'id':'ou_me'},'body':{'content':'{"text":"answer"}'}}
        self.assertTrue(verify_reply(reply,original,'answer','ou_me'))
        reply['parent_id']='other';self.assertFalse(verify_reply(reply,original,'answer','ou_me'))

    def test_thread_reply_accepts_platform_root_parent_only_same_thread(self):
        original={'message_id':'child','root_id':'root','thread_id':'thread','chat_id':'c'}
        reply={'chat_id':'c','parent_id':'root','thread_id':'thread','sender':{'id':'ou_me'},'body':{'content':'{"text":"answer"}'}}
        self.assertTrue(verify_reply(reply,original,'answer','ou_me'))
        reply['thread_id']='other';self.assertFalse(verify_reply(reply,original,'answer','ou_me'))


class SearchTests(unittest.TestCase):
    def transport(self, response):
        t = Transport({}, threading.Event())
        t.api = lambda *args, **kwargs: response
        return t

    def test_notice_is_returned_not_raised(self):
        notice = {'type': 'incomplete', 'reason': 'window too large'}
        t = self.transport({'items': [{'meta_data': {'message_id': 'om_1'}}], 'has_more': True,
                            'page_token': 'next', 'notice': notice})
        self.assertEqual(t.search(1, 2), {'ids': ['om_1'], 'next_token': 'next', 'notice': notice})

    def test_non_dict_notice_keeps_signal_and_type(self):
        t = self.transport({'items': [], 'has_more': False, 'notice': 'slow down'})
        result = t.search(1, 2)
        self.assertIsInstance(result['notice'], dict)
        self.assertIn('slow down', str(result['notice']))

    def test_full_page_without_token_is_protocol_error(self):
        for response in (
            {'items': [{'meta_data': {'message_id': 'om_1'}}], 'has_more': True},
            {'items': [{'meta_data': {'message_id': 'om_1'}}], 'has_more': True, 'page_token': 'same'},
        ):
            with self.subTest(response=response):
                with self.assertRaises(TransportError) as caught:
                    self.transport(response).search(1, 2, 'same')
                self.assertEqual(caught.exception.kind, 'protocol')

    def test_malformed_items_are_protocol_errors(self):
        for response in (
            {'items': None, 'has_more': False},
            {'items': [], 'notice': None},
            {'items': [{'meta_data': None}], 'has_more': False},
            {'items': [{'meta_data': {'message_id': 'not-an-om-id'}}], 'has_more': False},
            {'items': [{}], 'has_more': False},
        ):
            with self.subTest(response=response):
                with self.assertRaises(TransportError) as caught:
                    self.transport(response).search(1, 2)
                self.assertEqual(caught.exception.kind, 'protocol')


class SelfChatTests(unittest.TestCase):
    """认出"自己跟自己"的会话：p2p 且对方就是本人。"""

    def transport(self, pages, me='ou_me'):
        t = Transport({'target_open_id': me}, threading.Event())
        remaining = list(pages)
        t.run = lambda args, **kwargs: {'data': remaining.pop(0)}
        return t

    def test_the_chat_whose_other_side_is_me_is_recognised(self):
        t = self.transport([{'chats': [
            {'chat_id': 'oc_colleague', 'chat_mode': 'p2p', 'p2p_target_id': 'ou_other'},
            {'chat_id': 'oc_self', 'chat_mode': 'p2p', 'p2p_target_id': 'ou_me'},
        ], 'has_more': False}])
        self.assertEqual(t.self_chat_id(), 'oc_self')

    def test_it_pages_through_the_full_list(self):
        t = self.transport([
            {'chats': [{'chat_id': 'oc_a', 'chat_mode': 'p2p', 'p2p_target_id': 'ou_other'}],
             'has_more': True, 'page_token': 'p2'},
            {'chats': [{'chat_id': 'oc_self', 'chat_mode': 'p2p', 'p2p_target_id': 'ou_me'}],
             'has_more': False},
        ])
        self.assertEqual(t.self_chat_id(), 'oc_self')

    def test_no_candidate_is_a_protocol_error_not_a_guess(self):
        t = self.transport([{'chats': [], 'has_more': False}])
        with self.assertRaises(TransportError) as caught:
            t.self_chat_id()
        self.assertEqual(caught.exception.kind, 'protocol')
        self.assertFalse(caught.exception.retryable)

    def test_ambiguous_candidates_are_refused(self):
        t = self.transport([{'chats': [
            {'chat_id': 'oc_one', 'chat_mode': 'p2p', 'p2p_target_id': 'ou_me'},
            {'chat_id': 'oc_two', 'chat_mode': 'p2p', 'p2p_target_id': 'ou_me'},
        ], 'has_more': False}])
        with self.assertRaises(TransportError) as caught:
            t.self_chat_id()
        self.assertIn('认不准', str(caught.exception))


class RunParsingTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport({'lark_binary': '/fake/lark'}, threading.Event())
        self.transport._pace = lambda search=False: None

    def call(self, stdout='', stderr='', returncode=0, before_request=None):
        with patch('bridge.transport.subprocess.run', return_value=completed(stdout, stderr, returncode)):
            return self.transport.run(['api'], before_request=before_request)

    def test_success_returns_object(self):
        self.assertEqual(self.call(json.dumps({'ok': True, 'data': {'x': 1}})), {'ok': True, 'data': {'x': 1}})

    def test_stdout_noise_falls_back_to_stderr_error(self):
        payload = {'ok': False, 'error': {'type': 'authorization', 'code': 230002, 'message': 'token expired'}}
        with self.assertRaises(TransportError) as caught:
            self.call('Update available: 1.2.3\nSkill notices...\n', json.dumps(payload), returncode=1)
        self.assertEqual(caught.exception.kind, 'authorization')
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.code, 230002)

    def test_non_json_output_includes_output_prefix(self):
        with self.assertRaises(TransportError) as caught:
            self.call('not json at all', 'still not json', returncode=1)
        self.assertEqual(caught.exception.kind, 'protocol')
        self.assertTrue(str(caught.exception).startswith('Lark CLI returned non-JSON output: '))
        self.assertIn('not json at all', str(caught.exception))

    def test_non_object_json_is_protocol_error(self):
        for stdout in ('null', '[]', '"text"', '42'):
            with self.subTest(stdout=stdout):
                with self.assertRaises(TransportError) as caught:
                    self.call(stdout)
                self.assertEqual(caught.exception.kind, 'protocol')
                self.assertIn('non-object JSON', str(caught.exception))

    def test_exit_zero_error_envelope_is_classified(self):
        payload = {'error': {'type': 'authorization', 'code': 230002, 'message': 'no scope'}}
        with self.assertRaises(TransportError) as caught:
            self.call(json.dumps(payload), returncode=0)
        self.assertEqual(caught.exception.kind, 'authorization')
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(str(caught.exception), 'no scope')

    def test_error_envelope_with_ok_false_is_retryable_api(self):
        payload = {'ok': False, 'error': {'code': 123456, 'message': 'boom'}}
        with self.assertRaises(TransportError) as caught:
            self.call(json.dumps(payload), returncode=0)
        self.assertEqual(caught.exception.kind, 'api')
        self.assertTrue(caught.exception.retryable)

    def test_rate_limit_type_unknown_code_freezes_for_sixty_seconds(self):
        payload = {'ok': False, 'error': {'type': 'rate_limit', 'code': 1000006, 'message': 'slow down'}}
        with self.assertRaises(TransportError) as caught:
            self.call(json.dumps(payload), returncode=0)
        self.assertEqual(caught.exception.kind, 'rate_limit')
        self.assertEqual(caught.exception.code, 1000006)
        self.assertGreater(self.transport.next_request, time.monotonic() + 55)

    def test_rate_limit_recognized_by_code_and_message(self):
        for payload in (
            {'ok': False, 'error': {'type': 'api', 'code': 99991400, 'message': 'limited'}},
            {'ok': False, 'error': {'type': 'api', 'code': 230020, 'message': 'limited'}},
            {'ok': False, 'error': {'type': 'api', 'code': 42, 'message': 'Too many requests'}},
            {'ok': False, 'error': {'type': 'api', 'code': 42, 'message': 'please slow down'}},
        ):
            with self.subTest(payload=payload):
                self.transport.next_request = 0.0
                with self.assertRaises(TransportError) as caught:
                    self.call(json.dumps(payload), returncode=1)
                self.assertEqual(caught.exception.kind, 'rate_limit')
                self.assertGreater(self.transport.next_request, time.monotonic() + 55)

    def test_permanent_authorization_validation_codes(self):
        for error in ({'type': 'validation', 'code': 1, 'message': 'bad'},
                      {'type': 'api', 'code': 230002, 'message': 'bad'},
                      {'type': 'api', 'code': 230011, 'message': 'bad'},
                      {'type': 'api', 'code': 230050, 'message': 'bad'},
                      {'type': 'api', 'code': 231203, 'message': 'bad'},
                      {'type': 'api', 'code': 231204, 'message': 'bad'}):
            with self.subTest(error=error):
                with self.assertRaises(TransportError) as caught:
                    self.call(json.dumps({'ok': False, 'error': error}))
                self.assertFalse(caught.exception.retryable)

    def test_unknown_kind_or_code_stays_retryable(self):
        with self.assertRaises(TransportError) as caught:
            self.call(json.dumps({'ok': False, 'error': {'type': 'weird', 'code': 7, 'message': 'huh'}}))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.kind, 'weird')

    def test_non_string_error_is_ignored_for_classification(self):
        # Exit code alone must still fail, even when the error envelope is not an object.
        with self.assertRaises(TransportError) as caught:
            self.call(json.dumps({'ok': True, 'error': 'nope'}), returncode=1)
        self.assertEqual(caught.exception.kind, 'api')
        self.assertEqual(str(caught.exception), 'Lark request failed')


class IdentityTests(unittest.TestCase):
    SCOPES = 'im:chat:read im:message im:message.group_msg:get_as_user im:message.send_as_user search:message'

    def transport(self, payload):
        t = Transport({'target_open_id': 'ou_me'}, threading.Event())
        t.run = lambda args, search=False, before_request=None: payload
        return t

    def user(self, **extra):
        return dict({'openId': 'ou_me', 'available': True, 'scope': self.SCOPES, 'userName': 'me',
                     'tokenStatus': 'valid', 'expiresAt': 'x', 'refreshExpiresAt': 'y'}, **extra)

    def test_valid_identity_returns_public_fields(self):
        result = self.transport({'identities': {'user': self.user()}}).identity()
        self.assertEqual(result['openId'], 'ou_me')
        self.assertEqual(set(result), {'userName', 'openId', 'available', 'tokenStatus', 'expiresAt', 'refreshExpiresAt'})

    def test_wrong_user_or_unavailable_is_permanent_authorization(self):
        for payload in ({'identities': {'user': self.user(openId='ou_other')}},
                        {'identities': {'user': self.user(available=False)}}):
            with self.subTest(payload=payload):
                with self.assertRaises(TransportError) as caught:
                    self.transport(payload).identity()
                self.assertEqual(caught.exception.kind, 'authorization')
                self.assertFalse(caught.exception.retryable)

    def test_missing_scope_is_permanent_authorization(self):
        with self.assertRaises(TransportError) as caught:
            self.transport({'identities': {'user': self.user(scope='im:message')}}).identity()
        self.assertEqual(caught.exception.kind, 'authorization')
        self.assertFalse(caught.exception.retryable)
        self.assertIn('Missing scopes', str(caught.exception))

    def test_list_shaped_scope_is_accepted(self):
        result = self.transport({'identities': {'user': self.user(scope=self.SCOPES.split())}}).identity()
        self.assertEqual(result['openId'], 'ou_me')

    def test_non_text_scope_fails_closed_as_authorization(self):
        with self.assertRaises(TransportError) as caught:
            self.transport({'identities': {'user': self.user(scope=42)}}).identity()
        self.assertEqual(caught.exception.kind, 'authorization')
        self.assertFalse(caught.exception.retryable)

    def test_missing_envelope_is_authorization_not_key_error(self):
        for payload in ({}, {'identities': {}}, {'identities': None}, {'identities': {'user': None}}):
            with self.subTest(payload=payload):
                with self.assertRaises(TransportError) as caught:
                    self.transport(payload).identity()
                self.assertEqual(caught.exception.kind, 'authorization')


class UserScopeTests(unittest.TestCase):
    SCOPES = 'im:chat:read im:message im:message.group_msg:get_as_user im:message.send_as_user search:message'

    def transport(self, payload):
        t = Transport({'target_open_id': 'ou_me'}, threading.Event())
        t.run = lambda args, search=False, before_request=None: payload
        return t

    def test_reads_the_granted_scope_set(self):
        t = self.transport({'identities': {'user': {'scope': self.SCOPES}}})
        self.assertIn('im:message.group_msg:get_as_user', t.user_scopes())
        self.assertNotIn('im:message.p2p_msg:get_as_user', t.user_scopes())

    def test_list_shaped_scope_is_joined(self):
        t = self.transport({'identities': {'user': {'scope': self.SCOPES.split() + ['im:message.p2p_msg:get_as_user']}}})
        self.assertIn('im:message.p2p_msg:get_as_user', t.user_scopes())

    def test_unreadable_identity_is_an_empty_set_not_an_exception(self):
        for payload in ({}, {'identities': None}, {'identities': {'user': {'scope': 42}}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.transport(payload).user_scopes(), set())


class InventoryTests(unittest.TestCase):
    def test_requires_chat_id_on_every_row(self):
        t = Transport({}, threading.Event())
        t.run = lambda args, search=False, before_request=None: {'data': {'chats': [{'chat_id': 'oc_1'}, {'name': 'no id'}], 'has_more': False}}
        with self.assertRaises(TransportError) as caught:
            t.inventory()
        self.assertEqual(caught.exception.kind, 'protocol')
        self.assertIn('missing chat ID', str(caught.exception))

    def test_rejects_bad_data_envelope(self):
        for data in (None, [], {'chats': None}, {'chats': 'nope'}):
            with self.subTest(data=data):
                t = Transport({}, threading.Event())
                t.run = lambda args, search=False, before_request=None, data=data: {'data': data}
                with self.assertRaises(TransportError) as caught:
                    t.inventory()
                self.assertEqual(caught.exception.kind, 'protocol')

    def test_pages_until_has_more_false(self):
        t = Transport({}, threading.Event())
        calls = []

        def run(args, search=False, before_request=None):
            calls.append(args)
            if '--page-token' in args:
                return {'data': {'chats': [{'chat_id': 'oc_2'}], 'has_more': False}}
            return {'data': {'chats': [{'chat_id': 'oc_1'}], 'has_more': True, 'page_token': 'p2'}}

        t.run = run
        self.assertEqual([row['chat_id'] for row in t.inventory()], ['oc_1', 'oc_2'])
        self.assertEqual(len(calls), 2)

    def test_cursor_must_progress(self):
        t = Transport({}, threading.Event())
        t.run = lambda args, search=False, before_request=None: {'data': {'chats': [], 'has_more': True, 'page_token': 'p'}}
        with self.assertRaises(TransportError) as caught:
            t.inventory()
        self.assertEqual(caught.exception.kind, 'protocol')


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.anchor = 1792986423123
        self.chat = 'oc_chat'

    def transport(self, items, capture=None):
        t = Transport({}, threading.Event())

        def api(method, path, params, data=None, search=False):
            if capture is not None:
                capture.update(params)
            return {'items': items}

        t.api = api
        return t

    def message(self, **extra):
        return dict({'message_id': 'om_trigger', 'chat_id': self.chat, 'create_time': self.anchor, 'thread_id': None}, **extra)

    def item(self, mid, created, chat=None, deleted=False, text='x'):
        return {'message_id': mid, 'chat_id': chat or self.chat, 'create_time': created, 'deleted': deleted,
                'sender': {'sender_type': 'user'}, 'msg_type': 'text', 'body': {'content': json.dumps({'text': text})}}

    def test_window_is_anchored_to_trigger_and_excludes_trigger(self):
        items = [
            self.item('om_old', self.anchor - 600001),
            self.item('om_edge_old', self.anchor - 600000),
            self.item('om_mid', self.anchor - 5000),
            self.item('om_trigger', self.anchor),
            self.item('om_new', self.anchor + 60000),
            self.item('om_too_new', self.anchor + 60001),
            self.item('om_other_chat', self.anchor - 1000, chat='oc_other'),
            self.item('om_deleted', self.anchor - 2000, deleted=True),
        ]
        capture = {}
        result = self.transport(items, capture).context(self.message())
        self.assertEqual([row['message_id'] for row in result], ['om_edge_old', 'om_mid', 'om_new'])
        self.assertEqual(capture['container_id'], self.chat)
        self.assertEqual(capture['container_id_type'], 'chat')
        self.assertEqual(capture['page_size'], 50)
        self.assertEqual(capture['sort_type'], 'ByCreateTimeDesc')

    def test_thread_container_is_preserved(self):
        capture = {}
        self.transport([], capture).context(self.message(thread_id='th_1'))
        self.assertEqual(capture['container_id_type'], 'thread')
        self.assertEqual(capture['container_id'], 'th_1')

    def test_missing_create_time_falls_back_to_now(self):
        now = int(time.time() * 1000)
        items = [self.item('om_recent', now - 1000), self.item('om_ancient', now - 3600000)]
        result = self.transport(items).context({'message_id': 'om_trigger', 'chat_id': self.chat})
        self.assertEqual([row['message_id'] for row in result], ['om_recent'])

    def test_at_most_twenty_items_nearest_to_anchor(self):
        items = [self.item('om_%02d' % index, self.anchor - (60 - index) * 1000) for index in range(40)]
        result = self.transport(items).context(self.message())
        self.assertEqual(len(result), 20)
        self.assertEqual(result[0]['message_id'], 'om_20')
        self.assertEqual(result[-1]['message_id'], 'om_39')

    def test_text_budget_is_capped_at_sixteen_thousand(self):
        items = [self.item('om_%02d' % index, self.anchor - (20 - index) * 1000, text='a' * 1500) for index in range(20)]
        result = self.transport(items).context(self.message())
        self.assertEqual(sum(len(row['text']) for row in result), 16000)
        self.assertTrue(all(len(row['text']) <= 1200 for row in result))

    def history(self, items, on_request=None):
        """Fake history server: time filtering happens before descending pagination."""
        t = Transport({}, threading.Event())
        calls = []

        def api(method, path, params, **kwargs):
            calls.append(dict(params))
            if on_request:
                on_request(t, len(calls))
            rows = sorted(items, key=lambda item: int(item['create_time']), reverse=True)
            if params['container_id_type'] == 'chat':
                if 'start_time' in params:
                    rows = [row for row in rows if int(row['create_time']) >= int(params['start_time']) * 1000]
                if 'end_time' in params:
                    rows = [row for row in rows if int(row['create_time']) < int(params['end_time']) * 1000]
            offset = int(params.get('page_token', 0))
            page = rows[offset:offset + params['page_size']]
            more = offset + len(page) < len(rows)
            return {'items': page, 'has_more': more,
                    'page_token': str(offset + len(page)) if more else None}

        t.api = api
        return t, calls

    def test_chat_server_range_recovers_anchor_after_newer_tail(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n * 1000) for n in range(120)]
        items += [self.item('om_before', self.anchor - 1000), self.item('om_after', self.anchor + 60000),
                  self.item('om_too_new', self.anchor + 60001), self.item('om_old', self.anchor - 600001)]
        t, calls = self.history(items)
        self.assertEqual([r['message_id'] for r in t.context(self.message())], ['om_before', 'om_after'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['start_time'], str((self.anchor - 600000) // 1000))
        self.assertEqual(calls[0]['end_time'], str((self.anchor + 60000) // 1000 + 1))

    def test_thread_pages_past_newer_tail_and_filters_anchor_window(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n * 1000) for n in range(120)]
        items += [self.item('om_before', self.anchor - 1000), self.item('om_after', self.anchor + 60000),
                  self.item('om_trigger', self.anchor), self.item('om_old', self.anchor - 600001),
                  self.item('om_other', self.anchor - 2000, chat='oc_other'),
                  self.item('om_deleted', self.anchor - 3000, deleted=True)]
        t, calls = self.history(items)
        self.assertEqual([r['message_id'] for r in t.context(self.message(thread_id='th_1'))],
                         ['om_before', 'om_after'])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all('start_time' not in call and 'end_time' not in call for call in calls))

    def test_thread_continues_until_twenty_valid_items_then_stops(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n) for n in range(45)]
        items += [self.item('valid_%d' % n, self.anchor - n * 1000) for n in range(1, 100)]
        t, calls = self.history(items)
        result = t.context(self.message(thread_id='th_1'))
        self.assertEqual([r['message_id'] for r in result], ['valid_%d' % n for n in range(20, 0, -1)])
        self.assertEqual(len(calls), 2)

    def test_thread_stops_at_older_boundary(self):
        items = [self.item('old_%d' % n, self.anchor - 700000 - n) for n in range(100)]
        t, calls = self.history(items)
        self.assertEqual(t.context(self.message(thread_id='th_1')), [])
        self.assertEqual(len(calls), 1)

    def test_chat_pages_past_deleted_and_cross_chat_rows(self):
        items = [self.item('deleted_%d' % n, self.anchor - 1000 - n, deleted=True) for n in range(40)]
        items += [self.item('other_%d' % n, self.anchor - 2000 - n, chat='oc_other') for n in range(20)]
        items += [self.item('om_valid', self.anchor - 3000)]
        t, calls = self.history(items)
        self.assertEqual([r['message_id'] for r in t.context(self.message())], ['om_valid'])
        self.assertEqual(len(calls), 2)

    def test_empty_page_with_valid_cursor_can_reach_context(self):
        t = Transport({}, threading.Event())
        def api(method, path, params, **kwargs):
            if 'page_token' not in params:
                return {'items': [], 'has_more': True, 'page_token': 'next'}
            return {'items': [self.item('om_valid', self.anchor - 1000)], 'has_more': False}
        t.api = api
        self.assertEqual([r['message_id'] for r in t.context(self.message(thread_id='th_1'))], ['om_valid'])

    def test_twentieth_page_can_complete_context(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n) for n in range(950)]
        items += [self.item('om_valid', self.anchor - 1000)]
        t, calls = self.history(items)
        self.assertEqual([r['message_id'] for r in t.context(self.message(thread_id='th_1'))], ['om_valid'])
        self.assertEqual(len(calls), 20)

    def test_context_page_cap_reports_incomplete(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n) for n in range(1100)]
        t, calls = self.history(items)
        with self.assertRaises(TransportError) as caught:
            t.context(self.message(thread_id='th_1'))
        self.assertEqual(caught.exception.kind, 'context_incomplete')
        self.assertEqual(len(calls), 20)

    def test_context_time_budget_reports_incomplete_between_requests(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n) for n in range(100)]
        t, calls = self.history(items)
        with patch('bridge.transport.time.monotonic', side_effect=[0, 0, 31]):
            with self.assertRaises(TransportError) as caught:
                t.context(self.message(thread_id='th_1'))
        self.assertEqual(caught.exception.kind, 'context_incomplete')
        self.assertEqual(len(calls), 1)

    def test_nonprogressing_or_missing_cursor_reports_incomplete(self):
        for tokens in (['same', 'same'], [None], ['a', 'b', 'a']):
            with self.subTest(tokens=tokens):
                t = Transport({}, threading.Event())
                calls = []
                def api(*args, **kwargs):
                    token = tokens[len(calls)]
                    calls.append(token)
                    return {'items': [], 'has_more': True, 'page_token': token}
                t.api = api
                with self.assertRaises(TransportError) as caught:
                    t.context(self.message(thread_id='th_1'))
                self.assertEqual(caught.exception.kind, 'context_incomplete')
                self.assertEqual(len(calls), len(tokens))

    def test_cancellation_before_and_between_pages(self):
        items = [self.item('tail_%d' % n, self.anchor + 100000 + n) for n in range(100)]
        for before in (True, False):
            with self.subTest(before=before):
                t, calls = self.history(items, lambda transport, count: transport.stop.set())
                if before:
                    t.stop.set()
                with self.assertRaises(TransportError) as caught:
                    t.context(self.message(thread_id='th_1'))
                self.assertEqual(caught.exception.kind, 'stopped')
                self.assertEqual(len(calls), 0 if before else 1)

    def test_requires_chat_id(self):
        with self.assertRaises(TransportError) as caught:
            self.transport([]).context({'message_id': 'om_trigger', 'create_time': self.anchor})
        self.assertEqual(caught.exception.kind, 'protocol')


class ChatMessagePagingTests(unittest.TestCase):
    """控制通道窗口必须读全：只读一页 + 游标照推 = 超过 50 条的部分静默丢失。"""

    def transport(self, responses):
        transport = Transport({}, threading.Event())
        queue = list(responses)
        calls = []

        def api(method, path, params=None, data=None, search=False):
            calls.append(dict(params or {}))
            return queue.pop(0)
        transport.api = api
        return transport, calls

    def message(self, mid):
        return {'message_id': mid, 'chat_id': 'oc_self', 'create_time': '1000',
                'sender': {'id': 'ou_target'}, 'msg_type': 'text',
                'body': {'content': json.dumps({'text': 'x'})}}

    def test_follows_page_token_until_has_more_is_false(self):
        first = {'items': [self.message('om_1')], 'has_more': True, 'page_token': 'p2'}
        second = {'items': [self.message('om_2')], 'has_more': False}
        transport, calls = self.transport([first, second])
        messages, complete = transport.chat_messages('oc_self', 0, 10000)
        self.assertEqual([item['message_id'] for item in messages], ['om_1', 'om_2'])
        self.assertTrue(complete)
        self.assertNotIn('page_token', calls[0])
        self.assertEqual(calls[1]['page_token'], 'p2')

    def test_stops_at_max_pages_and_reports_incomplete(self):
        pages = [{'items': [self.message('om_1')], 'has_more': True, 'page_token': 'p1'},
                 {'items': [self.message('om_2')], 'has_more': True, 'page_token': 'p2'}]
        transport, calls = self.transport(pages)
        messages, complete = transport.chat_messages('oc_self', 0, 10000, max_pages=2)
        self.assertEqual([item['message_id'] for item in messages], ['om_1', 'om_2'])
        self.assertFalse(complete)
        self.assertEqual(len(calls), 2)

    def test_raises_when_page_token_does_not_advance(self):
        pages = [{'items': [self.message('om_1')], 'has_more': True, 'page_token': 'p1'},
                 {'items': [self.message('om_2')], 'has_more': True, 'page_token': 'p1'}]
        transport, _ = self.transport(pages)
        with self.assertRaises(TransportError) as caught:
            transport.chat_messages('oc_self', 0, 10000)
        self.assertEqual(caught.exception.kind, 'protocol')


if __name__=='__main__':unittest.main()


class SearchPacingTests(unittest.TestCase):
    """搜索接口的最小间隔必须是可配的、有实测依据的。

    为什么单独立几组用例：这个常数直接决定采集延迟的下限。写死 10 秒的时候，
    `live_interval_seconds` 从 12 调到 3 完全不生效——改了没效果，比改错更难发现。
    """

    def make(self, **config):
        from bridge.transport import Transport
        return Transport(dict(config), threading.Event())

    def test_the_default_interval_matches_the_measured_safe_rate(self):
        from bridge.transport import SEARCH_MIN_INTERVAL_SECONDS, Transport
        transport = Transport({}, threading.Event())
        self.assertEqual(transport.search_interval, SEARCH_MIN_INTERVAL_SECONDS)
        self.assertLessEqual(SEARCH_MIN_INTERVAL_SECONDS, 1.0,
                             '实测 1 秒间隔连发 10 次全部成功；更大的值只会增加采集延迟')

    def test_the_interval_is_configurable_per_machine(self):
        self.assertEqual(self.make(search_min_interval_seconds=3).search_interval, 3.0)
        self.assertEqual(self.make(search_min_interval_seconds='2.5').search_interval, 2.5)

    def test_a_bad_value_falls_back_instead_of_disabling_the_pace(self):
        for value in ('soon', None, -1):
            self.assertEqual(self.make(search_min_interval_seconds=value).search_interval,
                             self.make().search_interval)

    def test_two_searches_really_wait_the_configured_interval(self):
        # 真的用时间来量，而不是断言"下一次预约变大了"——后者在间隔为 0 时也会通过。
        transport = self.make(search_min_interval_seconds=0.3)
        transport._pace(search=True)
        started = time.monotonic()
        transport._pace(search=True)
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 2.0)

    def test_a_non_search_request_does_not_wait_for_the_search_interval(self):
        transport = self.make(search_min_interval_seconds=5)
        transport._pace(search=True)
        started = time.monotonic()
        transport._pace(search=False)  # 发消息不受搜索间隔影响
        self.assertLess(time.monotonic() - started, 1.0)
