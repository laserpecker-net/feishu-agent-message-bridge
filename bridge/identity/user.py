"""``as_user`` 身份：用**个人账号**的 lark-cli 收发消息（搜索轮询 + 用户 token）。

这是原本 ``bridge/transport.py`` 的实现整体搬过来的——PR-1 只搬家、不改行为：
参数、错误类型、重试与节流逻辑逐字保持不变。``bridge/transport.py`` 现在只是兼容壳
（老代码与测试继续 ``from bridge.transport import Transport``）。
"""
import hashlib
import json
import re
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from .base import Identity, TransportError


_UNPARSED = object()


def mentioned(message, target):
    for item in message.get('mentions') or []:
        identity = item.get('id')
        if identity == target or (isinstance(identity, dict) and identity.get('open_id') == target):
            return True
    return False


def _visible(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return '\n'.join(filter(None, (_visible(value) for value in node)))
    if not isinstance(node, dict):
        return ''
    if node.get('tag') == 'at':
        return '@' + str(node.get('user_name') or node.get('user_id') or '')
    if node.get('tag') in ('img', 'image'):
        return '[图片，尚未读取像素]'
    if isinstance(node.get('text'), str):
        return node['text']
    if 'title' in node or 'content' in node:
        return '\n'.join(filter(None, (_visible(node.get('title')), _visible(node.get('content')))))
    for locale in ('zh_cn', 'en_us', 'ja_jp'):
        if locale in node:
            return _visible(node[locale])
    return '\n'.join(filter(None, (_visible(value) for value in node.values())))


def message_text(message):
    content = message.get('body', {}).get('content', '{}')
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return '[无法解析消息正文]'
    visible = _visible(parsed).strip()
    return visible or '[%s 消息：当前未读取附件内容，请说明问题]' % message.get('msg_type', '未知')


def message_version(message):
    value = [message.get('body', {}).get('content'), message.get('update_time'), bool(message.get('deleted'))]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


#: 正文里的 at 标记有两种形态：我们发出去的原始标记 ``<at user_id="ou_x"></at>``，
#: 和回读时渲染出来的 ``@张三``。它们指同一件事，逐字比较必然不等——2026-11-18 统计：
#: 238 条不含 @ 的回复全部通过回读校验，14 条含 @ 的**一次都没有**通过，全部被误标成
#: 待人工处理（而飞书上其实已经发出去了）。所以比较按「结构」做，不按「字面」做。
_AT_TAG = re.compile(r'<at\s+user_id="[^"]*"[^>]*>.*?</at>', re.S)
_AT_TEXT = re.compile(r'@[^\s@]+')
#: 归一化占位符：只表示"这里有一个 @ 人"，不保留名字（名字会随群昵称变，身份才是事实）。
#: 刻意不含 `@`，否则第二道替换会把自己的产物再替换一次。
AT_PLACEHOLDER = '\x00at\x00'


def canonical_text(text):
    """把「@ 人」的两种写法归一到同一个占位符，其余文字原样保留。

    ``<at user_id="ou_1"></at>你好``、``@张三你好`` 归一后都是 ``\x00@\x00你好``——
    这样比较的是"发给他的话有没有被改动"，不是"飞书把 at 渲染成了名字还是 id"。
    """
    if not isinstance(text, str):
        return ''
    text = _AT_TAG.sub(AT_PLACEHOLDER, text)
    return _AT_TEXT.sub(AT_PLACEHOLDER, text)


def mention_ids(message_or_body):
    """正文里被 @ 的 id 集合：既认原始标记，也认回读消息的 ``mentions`` 字段。"""
    if isinstance(message_or_body, str):
        return set(re.findall(r'<at\s+user_id="([^"]*)"', message_or_body))
    ids = set()
    for item in message_or_body.get('mentions') or []:
        identity = item.get('id')
        if isinstance(identity, dict):
            identity = identity.get('open_id') or identity.get('user_id') or identity.get('union_id')
        if isinstance(identity, str) and identity:
            ids.add(identity)
    return ids


def verify_reply_reason(reply, original, body, target):
    """回读校验：通过返回空串，不通过返回给运维看的原因（写进 outbox.error）。

    真正要比的是三件事：**这条回复是不是发在我回的那条下面、是不是我的身份发的、
    内容有没有被改动**。@ 的写法差异不算改动，所以正文按 ``canonical_text`` 比，
    另外单独核对"@ 了谁"这个集合——名字可以变，id 不能少。
    """
    if reply.get('deleted') or reply.get('chat_id') != original.get('chat_id'):
        return 'Reply deleted or wrong chat; no resend'
    if reply.get('sender', {}).get('id') != target:
        return 'Reply sender mismatch; no resend'
    if canonical_text(message_text(reply)) != canonical_text(body):
        return 'Reply readback mismatch; no resend'
    sent_to, got = mention_ids(body), mention_ids(reply)
    if sent_to != got:
        # 正文相等但 @ 的对象不对：可能是飞书吞了 at，也可能是我们发错了人。宁可人工看。
        return 'Reply mentions mismatch; no resend'
    thread = original.get('thread_id')
    if thread:
        if reply.get('thread_id') != thread or reply.get('parent_id') not in {
                original['message_id'], original.get('root_id') or original['message_id']}:
            return 'Reply thread mismatch; no resend'
    elif reply.get('parent_id') != original['message_id']:
        return 'Reply thread mismatch; no resend'
    return ''


def verify_reply(reply, original, body, target):
    return not verify_reply_reason(reply, original, body, target)


#: 搜索接口的最小间隔（秒）。为什么不再写死 10 秒：那个数是客户端自己猜的保险，
#: 实测 10 次连续调用、间隔 1 秒，全部成功（每次 0.6～1.7 秒，无 230020）。而它直接决定
#: 采集延迟的下限——扫描间隔配得再小，也压不到这个数以下。真限流时服务端会用 230020 明说，
#: 代码里本来就有"限流则冻结 60 秒"的权威处理，唯一事实交给它。
SEARCH_MIN_INTERVAL_SECONDS = 1.0


class UserIdentity(Identity):
    def __init__(self, config, stop):
        self.config, self.stop = config, stop
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.next_search = 0.0
        self.search_interval = self._search_interval()
        self.env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')

    def _search_interval(self):
        value = (self.config or {}).get('search_min_interval_seconds', SEARCH_MIN_INTERVAL_SECONDS)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return SEARCH_MIN_INTERVAL_SECONDS
        return value if value >= 0 else SEARCH_MIN_INTERVAL_SECONDS

    def _pace(self, search=False):
        while not self.stop.is_set():
            with self.lock:
                now = time.monotonic()
                due = max(self.next_request, self.next_search if search else 0)
                if due <= now:
                    self.next_request = now + 0.3
                    if search:
                        self.next_search = now + self.search_interval
                    return
            self.stop.wait(min(0.5, max(0.01, due - now)))
        raise TransportError('service stopping', 'stopped')

    def run(self, args, search=False, before_request=None):
        self._pace(search)
        if self.stop.is_set():
            raise TransportError('service stopping', 'stopped')
        if before_request:
            before_request()
        try:
            process = subprocess.run([self.config['lark_binary']] + args, capture_output=True,
                                     text=True, env=self.env, timeout=35)
        except subprocess.TimeoutExpired:
            raise TransportError('Lark request timed out; outcome may be unknown for sends')
        # The CLI can print update/skill notices before the real JSON payload; prefer stdout,
        # then fall back to stderr so a machine-readable error is not lost as "non-JSON".
        result = _UNPARSED
        for candidate in (process.stdout, process.stderr):
            if not (candidate or '').strip():
                continue
            try:
                result = json.loads(candidate)
                break
            except ValueError:
                continue
        if result is _UNPARSED:
            noise = (process.stdout or process.stderr or '').strip()
            raise TransportError('Lark CLI returned non-JSON output: ' + noise[:200], 'protocol')
        if not isinstance(result, dict):
            raise TransportError('Lark CLI returned non-object JSON', 'protocol')
        error = result.get('error') if isinstance(result.get('error'), dict) else {}
        # `ok is not True` + an error envelope is a failure even when the CLI exits 0.
        failed = (process.returncode != 0 or result.get('ok') is False
                  or (process.returncode == 0 and result.get('ok') is not True
                      and isinstance(result.get('error'), dict)))
        if failed:
            code = error.get('code')
            kind = error.get('type') if isinstance(error.get('type'), str) else 'api'
            message = str(error.get('message', 'Lark request failed'))[:500]
            rate_limited = (kind == 'rate_limit' or code in (230020, 99991400)
                            or any(word in message.lower() for word in ('rate limit', 'frequency limit', 'too many', 'slow down')))
            if rate_limited:
                with self.lock:
                    self.next_request = max(self.next_request, time.monotonic() + 60)
                raise TransportError(message, 'rate_limit', code)
            permanent = kind in ('authorization', 'validation') or code in (230002, 230011, 230050, 231203, 231204)
            raise TransportError(message, kind, code, not permanent)
        return result

    def api(self, method, path, params=None, data=None, search=False, before_request=None):
        args = ['api', method, path, '--as', self.api_identity]
        if params is not None:
            args += ['--params', json.dumps(params)]
        if data is not None:
            args += ['--data', json.dumps(data, ensure_ascii=False)]
        result = self.run(args, search, before_request)
        value = result.get('data')
        if not isinstance(value, dict):
            raise TransportError('Missing API data envelope', 'protocol')
        return value

    def _auth_user(self):
        """`auth status` 里的 user 段；读不出就是空 dict（永不抛，让调用方决定怎么失败）。"""
        result = self.run(['auth', 'status', '--json'])
        identities = result.get('identities')
        user = identities.get('user') if isinstance(identities, dict) else None
        return user if isinstance(user, dict) else {}

    def user_scopes(self):
        """当前用户 token 真正授权到的 scope 集合。

        用来回答"这条通道离得了它吗"这类前置问题：教学私聊要读 p2p 消息，缺
        ``im:message.p2p_msg:get_as_user`` 时检索能扫到消息 id、取正文那一拍 access denied
        （2026-11-06 真机踩到）。读不出/形状不对一律当空集——调用方按"缺"处理，宁可拒绝也不猜。
        """
        scope = self._auth_user().get('scope')
        # TR-06: tolerate a list-shaped scope instead of raising AttributeError out of the error taxonomy.
        if isinstance(scope, list):
            scope = ' '.join(item for item in scope if isinstance(item, str))
        return set(scope.split()) if isinstance(scope, str) else set()

    def identity(self):
        user = self._auth_user()
        if user.get('openId') != self.config['target_open_id'] or not user.get('available'):
            raise TransportError('Expected Feishu user is not authenticated', 'authorization', retryable=False)
        needed = {'im:chat:read', 'im:message', 'im:message.group_msg:get_as_user', 'im:message.send_as_user', 'search:message'}
        scopes = self.user_scopes()
        if not needed.issubset(scopes):
            raise TransportError('Missing scopes: ' + ', '.join(sorted(needed - scopes)), 'authorization', retryable=False)
        return {key: user.get(key) for key in ('userName', 'openId', 'available', 'tokenStatus', 'expiresAt', 'refreshExpiresAt')}

    def inventory(self):
        rows, token, seen = [], None, set()
        while True:
            args = ['im', '+chat-list', '--as', 'user', '--page-size', '100', '--sort', 'create_time', '--format', 'json']
            if token:
                args += ['--page-token', token]
            data = self.run(args).get('data')
            if not isinstance(data, dict) or not isinstance(data.get('chats'), list):
                raise TransportError('Unrecognized group inventory response', 'protocol')
            for chat in data['chats']:
                chat_id = chat.get('chat_id') if isinstance(chat, dict) else None
                if not isinstance(chat_id, str) or not chat_id:
                    raise TransportError('Group inventory missing chat ID', 'protocol')
                rows.append(chat)
            if not data.get('has_more'):
                return rows
            token = data.get('page_token')
            if not isinstance(token, str) or not token or token in seen:
                raise TransportError('Group inventory cursor did not progress', 'protocol')
            seen.add(token)

    def p2p_chats(self):
        """列当前账号的一对一会话（`--types p2p`）。

        只有一个用途：认出"自己跟自己"那个会话（`p2p_target_id` 等于本人 open_id）。
        别拿它当入参去搜消息——搜索只接受明确给出的 `chat_id`，私聊不会被批量卷进来。
        """
        rows, token, seen = [], None, set()
        while True:
            args = ['im', '+chat-list', '--as', 'user', '--types', 'p2p', '--page-size', '100',
                    '--format', 'json']
            if token:
                args += ['--page-token', token]
            data = self.run(args).get('data')
            if not isinstance(data, dict) or not isinstance(data.get('chats'), list):
                raise TransportError('Unrecognized p2p chat response', 'protocol')
            rows.extend(chat for chat in data['chats'] if isinstance(chat, dict))
            if not data.get('has_more'):
                return rows
            token = data.get('page_token')
            if not isinstance(token, str) or not token or token in seen:
                raise TransportError('p2p chat cursor did not progress', 'protocol')
            seen.add(token)

    def self_chat_id(self):
        """认出"自己跟自己"的会话：p2p 且对方就是本人。

        为什么值得有这个：私聊要显式登记才被采集（搜索只搜群），而让人自己去飞书里翻
        `oc_…` 是这道门槛上唯一的摩擦；这个会话是可算出来的，就不该让人手工抄。
        找不到或不止一个时宁可报错也不猜——配错会话等于把别人的私聊当教材通道。
        """
        me = (self.config or {}).get('target_open_id')
        if not isinstance(me, str) or not me:
            raise TransportError('config 里没有 target_open_id，认不出本人会话', 'protocol',
                                 retryable=False)
        found = [chat for chat in self.p2p_chats()
                 if chat.get('chat_mode') == 'p2p' and chat.get('p2p_target_id') == me]
        if len(found) == 1:
            return found[0]['chat_id']
        if not found:
            raise TransportError('没找到"自己跟自己"的会话：在飞书里给自己发一条消息，再试一次',
                                 'protocol', retryable=False)
        raise TransportError('有 %d 个会话的对方都是本人，认不准：%s'
                             % (len(found), '、'.join(chat.get('chat_id') or '?' for chat in found)),
                             'protocol', retryable=False)

    def search(self, start_ms, end_ms, page_token=None, chat_id=None):
        """搜「@ 了我」的消息。默认只搜群；给 chat_id 时精确搜那一个会话。

        传 chat_id 时不带 chat_type：教材私聊是 p2p，而 p2p 消息进不来正是这个通道要解决的
        那个空洞。范围收窄到单个会话（实测 `filter.chat_ids` 生效），不会把别的私聊卷进来。
        """
        def iso(value):
            # The installed search endpoint rejects fractional-second RFC3339 timestamps.
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec='seconds')
        params = {'page_size': 50}
        if page_token:
            params['page_token'] = page_token
        scope = {'chat_ids': [chat_id]} if chat_id else {'chat_type': 'group'}
        body = {'query': '', 'filter': dict(scope, is_at_me=True,
                time_range={'start_time': iso(start_ms), 'end_time': iso(end_ms)})}
        data = self.api('POST', '/open-apis/im/v1/messages/search', params, body, True)
        if not isinstance(data.get('items'), list) or 'has_more' not in data:
            raise TransportError('Unrecognized raw search response', 'protocol')
        notice = data.get('notice')
        if notice is not None and not isinstance(notice, dict):
            # Keep the type contract for callers while preserving the incomplete-result signal.
            notice = {'value': str(notice)[:250]}
        ids = []
        for item in data['items']:
            meta = item.get('meta_data') if isinstance(item, dict) else None
            mid = meta.get('message_id') if isinstance(meta, dict) else None
            if not isinstance(mid, str) or not mid.startswith('om_'):
                raise TransportError('Search result missing message ID', 'protocol')
            ids.append(mid)
        token = data.get('page_token') if data['has_more'] else None
        if data['has_more'] and (not token or token == page_token):
            raise TransportError('Search cursor did not progress', 'protocol')
        return {'ids': ids, 'next_token': token, 'notice': notice}

    def get_message(self, mid):
        data = self.api('GET', '/open-apis/im/v1/messages/' + mid)
        for item in data.get('items') or []:
            if item.get('message_id') == mid:
                return item
        raise TransportError('Message detail absent from response', 'protocol')

    def probe_group(self, chat):
        self.api('GET', '/open-apis/im/v1/messages', {'container_id_type': 'chat', 'container_id': chat, 'page_size': 1})

    def chat_info(self, chat_id):
        """会话元信息（不含消息）：教学会话必须是本人私聊，靠 ``chat_mode == 'p2p'`` 判定。"""
        if not isinstance(chat_id, str) or not chat_id.startswith('oc_'):
            raise TransportError('Chat info needs a chat_id', 'protocol')
        return self.api('GET', '/open-apis/im/v1/chats/%s' % chat_id)

    def chat_messages(self, chat_id, start_ms, end_ms, page_size=50, max_pages=20):
        """按时间窗读取指定会话的消息（用于自聊控制通道）。

        `search()` 固定带 ``is_at_me + chat_type=group`` 过滤，看不到自聊（p2p）消息，
        所以挂起开关的指令入口必须走"列出会话消息"这个接口。

        单页最多 50 条，超过一页时必须跟着 ``page_token`` 读完；只读一页而把游标推到窗口末尾
        等于静默丢弃消息。返回 ``(messages, complete)``：``complete`` 为假表示页数到了
        ``max_pages`` 仍没读完，调用方应把游标停在已读到的位置，而不是窗口末尾。
        """
        base = {'container_id_type': 'chat', 'container_id': chat_id,
                'page_size': max(1, min(50, int(page_size))), 'sort_type': 'ByCreateTimeAsc',
                'start_time': str(int(start_ms // 1000)), 'end_time': str(int(end_ms // 1000))}
        messages = []
        token = None
        for _ in range(max(1, int(max_pages))):
            params = dict(base)
            if token:
                params['page_token'] = token
            data = self.api('GET', '/open-apis/im/v1/messages', params)
            items = data.get('items') if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise TransportError('Unrecognized chat message list response', 'protocol')
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = item.get('message_id')
                if not isinstance(mid, str) or not mid.startswith('om_'):
                    raise TransportError('Chat message list missing message ID', 'protocol')
                messages.append(item)
            if not data.get('has_more'):
                return messages, True
            next_token = data.get('page_token')
            if not isinstance(next_token, str) or not next_token.strip() or next_token == token:
                raise TransportError('Chat message pagination did not advance', 'protocol')
            token = next_token
        return messages, False

    def send_text(self, chat_id, body, dedupe_uuid=None):
        """直接向会话发一条文本（回执用；主动 say 走 outbox 时也用它，由 outbox 给幂等键）。

        ``dedupe_uuid`` 不给就按时间生成（回执是一次性的，重试语义由调用方负责）；
        给了就用它——outbox 里每一行有稳定的 ``uuid``，重试不会在群里发出第二条。
        """
        request_uuid = (dedupe_uuid if isinstance(dedupe_uuid, str) and dedupe_uuid
                        else uuid.uuid5(uuid.NAMESPACE_URL, 'bridge-notice:%s:%s'
                                        % (chat_id, time.time())).hex)
        data = self.api('POST', '/open-apis/im/v1/messages', {'receive_id_type': 'chat_id'},
                        data={'receive_id': chat_id, 'msg_type': 'text',
                              'content': json.dumps({'text': body}, ensure_ascii=False),
                              'uuid': request_uuid})
        mid = data.get('message_id') if isinstance(data, dict) else None
        if not isinstance(mid, str) or not mid.startswith('om_'):
            raise TransportError('Notice send response missing message ID', 'protocol')
        return mid

    def send_dm(self, open_id, body, dedupe_key=None):
        """给某个用户发一条私聊（主动性通知：求助/确认/漏答提醒；不经过本地 outbox）。

        与 ``reply`` 的区别是它不是"对某条消息的回复"，所以没有原消息可回；与 ``send_text``
        的区别是按 ``receive_id_type=open_id`` 发（还不知道 dm 会话 id 时也能发，响应里带
        ``chat_id``，调用方可以记下来做后续轮询）。

        ``dedupe_key`` 参与请求 uuid：同一个 key 在飞书侧 1 小时内只会落一条消息，
        本地还有更长的去重窗口（``bridge/notify.py``），所以"重试"不会变成"刷屏"。
        """
        if not isinstance(open_id, str) or not open_id.startswith('ou_'):
            raise TransportError('Direct message requires a user open_id', 'protocol')
        stamp = dedupe_key if isinstance(dedupe_key, str) and dedupe_key else '%s:%.3f' % (open_id, time.time())
        data = self.api('POST', '/open-apis/im/v1/messages', {'receive_id_type': 'open_id'},
                        data={'receive_id': open_id, 'msg_type': 'text',
                              'content': json.dumps({'text': body}, ensure_ascii=False),
                              'uuid': uuid.uuid5(uuid.NAMESPACE_URL, 'bridge-dm:%s' % stamp).hex})
        mid = data.get('message_id') if isinstance(data, dict) else None
        if not isinstance(mid, str) or not mid.startswith('om_'):
            raise TransportError('Direct message response missing message ID', 'protocol')
        chat_id = data.get('chat_id') if isinstance(data, dict) else None
        return {'message_id': mid, 'chat_id': chat_id if isinstance(chat_id, str) else None}

    def context(self, message):
        # P1-04: anchor the window to the trigger message so retries/backfill do not hand the
        # model the chat's current tail instead of the conversation around the question.
        try:
            anchor = int(message['create_time'])
        except (KeyError, TypeError, ValueError):
            anchor = int(time.time() * 1000)
        start, end = anchor - 600000, anchor + 60000
        chat_id = message.get('chat_id')
        if not isinstance(chat_id, str) or not chat_id:
            raise TransportError('Message context requires a chat ID', 'protocol')
        thread = message.get('thread_id')
        params = {'container_id_type': 'thread' if thread else 'chat',
                  'container_id': thread or chat_id, 'page_size': 50, 'sort_type': 'ByCreateTimeDesc'}
        if not thread:
            # Server history accepts seconds; widen the edges and filter exact milliseconds below.
            params.update(start_time=str(start // 1000), end_time=str(end // 1000 + 1))
        deadline = time.monotonic() + 30
        window, seen_ids, seen_tokens = [], set(), set()
        for page in range(20):
            if self.stop.is_set():
                raise TransportError('service stopping', 'stopped')
            if time.monotonic() >= deadline:
                raise TransportError('Message context time budget exhausted', 'context_incomplete')
            data = self.api('GET', '/open-apis/im/v1/messages', params)
            if self.stop.is_set():
                raise TransportError('service stopping', 'stopped')
            reached_start = False
            for item in data.get('items') or []:
                if not isinstance(item, dict) or item.get('chat_id') != chat_id:
                    continue
                try:
                    created = int(item.get('create_time'))
                except (TypeError, ValueError):
                    continue
                reached_start = reached_start or created < start
                mid = item.get('message_id')
                if (item.get('deleted') or not isinstance(mid, str) or not mid
                        or mid == message.get('message_id') or created == anchor
                        or created < start or created > end or mid in seen_ids):
                    continue
                seen_ids.add(mid)
                window.append((created, item))
            # Descending pages already contain the newest usable 20 once this count is met.
            if len(window) >= 20 or reached_start or not data.get('has_more'):
                break
            token = data.get('page_token')
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise TransportError('Message context cursor did not progress', 'context_incomplete')
            seen_tokens.add(token)
            params['page_token'] = token
        else:
            raise TransportError('Message context page budget exhausted', 'context_incomplete')
        # Keep the items nearest to the anchor (most recent first in the server page), then emit oldest-first.
        window.sort(key=lambda pair: pair[0])
        result, remaining = [], 16000
        for _, item in window[-20:]:
            text = message_text(item)[:min(1200, remaining)]
            result.append({'message_id': item['message_id'], 'sender_type': item.get('sender', {}).get('sender_type'), 'text': text})
            remaining -= len(text)
            if remaining <= 0:
                break
        return result

    def delete_message(self, message_id):
        """撤回本人发出的一条消息： ``DELETE /open-apis/im/v1/messages/{message_id}``。

        飞书侧只允许撤回本人发出且未超时限的消息；非本人/超时/已删都会作为 API 错误返回，
        由 :meth:`run` 的既有分类决定重试与否（``validation``/``authorization`` → 永久失败，
        交给 outbox 的 ``needs_attention`` 让人看，不静默重试）。
        """
        if not isinstance(message_id, str) or not message_id.startswith('om_'):
            raise TransportError('Recall requires a message ID', 'protocol')
        self.api('DELETE', '/open-apis/im/v1/messages/' + message_id)
        return True

    def reply(self, original, body, uuid, before_send=None):
        data = self.api('POST', '/open-apis/im/v1/messages/' + original['message_id'] + '/reply',
                        data={'msg_type': 'text', 'content': json.dumps({'text': body}, ensure_ascii=False),
                              'uuid': uuid, 'reply_in_thread': bool(original.get('thread_id'))}, before_request=before_send)
        mid = data.get('message_id')
        if not isinstance(mid, str) or not mid.startswith('om_'):
            raise TransportError('Send response did not include message ID', 'protocol')
        return mid

    # -- Identity 接口（PR-1：只此一种实现；bot 见 PR-2） --------------------
    mode = 'user'
    needs_self_guard = True

    #: lark-cli 的 `--as` 取值：用户身份；机器人身份覆写成 'bot'
    api_identity = 'user'

    def collect(self, scan_name, start_ms, end_ms):
        """``as_user`` 没有"对账窗口"：搜索轮询本身就是采集。"""
        return None

    def start_channel(self, on_message=None):
        """``as_user`` 没有长连接：采集由 service.collector 的搜索轮询驱动。"""
        return None

    def stop_channel(self):
        return None

    def is_owner(self, sender_id):
        return bool(sender_id) and sender_id == self.config.get('target_open_id')

    # -- 通道与自证 ---------------------------------------------------------
    def self_sender_id(self):
        return self.config.get('target_open_id')

    def command_chat_ok(self, chat_id, *, self_chat_ids=(), chat_type=None, known_group=False):
        """个人账号的命令通道，**与原语义逐字一致**：白名单自聊群，或"不在群册里的私聊"。

        为什么保留"不在群册"这条：本机与机器人的一对一私聊不在群册里，个人账号也在那里
        跟桥说话（真机上就是这么用的）。收紧成"只认白名单"会改变既有行为，PR-3 不允许。
        """
        if chat_id in set(self_chat_ids or ()):
            return True
        return not known_group

    def control_chat_ids(self, config, teach_chat=None):
        from ..service import self_chat_ids as _self_chat_ids  # 延迟导入避免循环
        chats = set(_self_chat_ids(config))
        if teach_chat:
            chats.add(teach_chat)
        return chats

    def is_mentioned(self, message):
        return mentioned(message, self.config.get('target_open_id'))

    def send(self, chat_id, body, kind='text', dedupe_key=None):
        if kind == 'dm':
            return self.send_dm(chat_id, body, dedupe_key=dedupe_key)
        return self.send_text(chat_id, body, dedupe_uuid=dedupe_key)

    def whoami(self):
        return {'mode': self.mode, 'open_id': self.config.get('target_open_id'),
                'display': self.config.get('signature') or ''}

    def describe(self):
        target = self.config.get('target_open_id') or ''
        masked = (target[:4] + '…' + target[-2:]) if len(target) >= 8 else ('已配置(短)' if target else '未配置')
        return [('identity', 'mode=user open_id=%s' % masked, bool(target)),
                ('identity_channel', 'poll', True)]
