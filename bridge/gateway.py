"""HTTP event gateway: let consumers on other machines (e.g. Windows) use the event channel.

The gateway is a thin, token-authenticated JSON/HTTP wrapper around the same
``Store`` APIs that ``feishu-bridge events`` uses. It never talks to Feishu or
lark-cli itself: claims are leases in SQLite, replies go into the immutable
outbox and are sent by the bridge's sender thread.

Security model:
- bind to loopback by default; ``--allow-remote`` is required for any other bind
- a 48-hex shared token, stored in ``<home>/gateway-token`` with mode 0600
- constant-time token comparison; no request bodies are logged
- request bodies capped at ``MAX_BODY`` bytes; JSON errors never echo bodies

The dispatch layer is a pure function of (method, path, headers, body) so it is
unit-testable without sockets.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from . import memory
from .events_view import event_view, parse_payload
from .store import Store, reply_uuid

TOKEN_FILENAME = 'gateway-token'
MAX_BODY = 64 * 1024
TOKEN_HEX = re.compile(r'^[0-9a-f]{32,128}$')
LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost')


class GatewayError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def token_path(home):
    return Path(home) / TOKEN_FILENAME


def read_token(path):
    path = Path(path)
    try:
        token = path.read_text().strip()
    except OSError as error:
        raise RuntimeError('cannot read gateway token %s: %s' % (path, error))
    if not TOKEN_HEX.match(token):
        raise RuntimeError('gateway token %s is malformed (expected 32..128 lowercase hex)' % path)
    return token


def generate_token(home, rotate=False, path=None):
    """Create (or read) the shared token. Returns ``(token, path, created)``."""
    if path is None:
        path = token_path(home)
    path = Path(path)
    if path.exists() and not rotate:
        return read_token(path), path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(token + '\n')
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return token, path, True


def health(port, host='127.0.0.1', timeout=1.5):
    """探一次回环网关的 ``/health``；返回 ``(ok, detail)``。

    为什么放在这里：安装器（装完就要知道它通没通）与 ``doctor``（体检）判的是**同一件事**，
    两处各写一份探针，过几个月一定一边改一边忘。判据只有一条：GET /health → 200。
    """
    import urllib.error
    import urllib.request
    url = 'http://%s:%s/health' % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(200).decode('utf-8', 'replace')
            if response.status == 200 and '"ok"' in body:
                return True, '%s → 200' % url
            return False, '%s → %s %s' % (url, response.status, body[:60])
    except urllib.error.URLError as error:
        return False, '%s → %s' % (url, error.reason)
    except (OSError, ValueError) as error:
        return False, '%s → %s' % (url, type(error).__name__)


def _int_field(body, name, default=None, minimum=None, maximum=None):
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayError(400, 'bad_request', '%s must be an integer' % name)
    if minimum is not None and value < minimum:
        raise GatewayError(400, 'bad_request', '%s must be >= %s' % (name, minimum))
    if maximum is not None and value > maximum:
        raise GatewayError(400, 'bad_request', '%s must be <= %s' % (name, maximum))
    return value


def _derived_uuid(scope):
    """没有调用方 uuid 时的默认幂等键：同一来源 + 同一内容重放不会多写一行。"""
    return hashlib.sha256(('bridge-outbox:' + scope).encode('utf-8')).hexdigest()[:40]


def _str_field(body, name, required=True):
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GatewayError(400, 'bad_request', '%s must be a nonempty string' % name)
    return value


def _read_config(home):
    try:
        data = json.loads((Path(home) / 'config.json').read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_external(home):
    try:
        data = json.loads((Path(home) / 'external.json').read_text())
    except (OSError, ValueError):
        return {'enabled': False, 'reply_timeout_seconds': 600}
    enabled = bool(data.get('enabled')) if isinstance(data, dict) else False
    timeout = data.get('reply_timeout_seconds') if isinstance(data, dict) else 600
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        timeout = 600
    return {'enabled': enabled, 'reply_timeout_seconds': timeout}


class Gateway:
    def __init__(self, store, home, token, clock=time.time):
        self.store = store
        self.home = Path(home)
        self.token = token
        self.clock = clock

    # -- auth ---------------------------------------------------------------
    def _authorize(self, headers):
        supplied = ''
        authorization = headers.get('authorization') or ''
        if authorization.lower().startswith('bearer '):
            supplied = authorization[7:].strip()
        if not supplied:
            supplied = headers.get('x-bridge-token') or ''
        if not supplied or not hmac.compare_digest(str(supplied), str(self.token)):
            raise GatewayError(401, 'unauthorized', 'missing or invalid gateway token')

    # -- dispatch -----------------------------------------------------------
    def dispatch(self, method, target, headers, body=b'', remote=None):
        self._remote = remote
        headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        method = (method or 'GET').upper()
        try:
            if isinstance(body, str):
                body = body.encode('utf-8')
            body = body or b''
            if len(body) > MAX_BODY:
                raise GatewayError(413, 'payload_too_large', 'request body exceeds %d bytes' % MAX_BODY)
            parts = urlsplit(target)
            path = '/' + parts.path.strip('/')
            query = parse_qs(parts.query)
            if path == '/health' and method == 'GET':
                return 200, {'ok': True, 'service': 'feishu-bridge-gateway', 'time': self.clock()}
            self._authorize(headers)
            return self._route(method, path, query, headers, body)
        except GatewayError as error:
            return error.status, {'ok': False, 'error': {'code': error.code, 'message': error.message}}
        except Exception as error:  # pragma: no cover - defensive: never leak internals
            return 500, {'ok': False, 'error': {'code': 'internal_error',
                                                'message': '%s' % type(error).__name__}}

    def _json_body(self, body):
        if not body:
            return {}
        try:
            data = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise GatewayError(400, 'bad_json', 'request body must be a UTF-8 JSON object')
        if not isinstance(data, dict):
            raise GatewayError(400, 'bad_json', 'request body must be a JSON object')
        return data

    # -- 推送型身份的入口（as_bot 的 sidecar）--------------------------------
    LOOPBACK = ('127.0.0.1', '::1', '::ffff:127.0.0.1')

    def _require_loopback(self):
        remote = getattr(self, '_remote', None)
        if remote not in self.LOOPBACK:
            raise GatewayError(403, 'forbidden',
                               'ingest is loopback-only (got %r); 长连接 sidecar 必须与桥同机'
                               % (remote,))

    @staticmethod
    def ingest_message_ids(body):
        """从 sidecar 转发的事件里取消息 id。只认三种形状，**不猜**：raw event / 单 id / 批量。"""
        if not isinstance(body, dict):
            raise GatewayError(400, 'bad_json', 'body must be an object')
        single = body.get('message_id')
        if isinstance(single, str) and single.startswith('om_'):
            return [single]
        batch = body.get('message_ids')
        if isinstance(batch, list) and batch:
            ids = [mid for mid in batch if isinstance(mid, str) and mid.startswith('om_')]
            if not ids:
                raise GatewayError(400, 'bad_request', 'message_ids has no om_ id')
            return ids
        event = body.get('event') if isinstance(body.get('event'), dict) else body
        message = event.get('message') if isinstance(event, dict) else None
        if isinstance(message, dict):
            mid = message.get('message_id')
            if isinstance(mid, str) and mid.startswith('om_'):
                return [mid]
        raise GatewayError(400, 'bad_request',
                           'no message id in payload (expected message_id / message_ids / event.message.message_id)')

    def _ingest(self, body):
        self._require_loopback()
        ids = self.ingest_message_ids(self._json_body(body))
        result = self.store.enqueue_messages(ids, self.clock())
        return 200, {'ok': True, 'queued': result['queued'], 'duplicates': result['duplicates']}

    def _ingest_status(self):
        self._require_loopback()
        return 200, {'ok': True, 'pending': self.store.inbox_pending_count()
                     if hasattr(self.store, 'inbox_pending_count') else None}

    def _route(self, method, path, query, headers, body):
        segments = [segment for segment in path.split('/') if segment]
        if segments[:2] == ['v1', 'ingest']:
            rest = segments[2:]
            if rest == []:
                if method != 'POST':
                    raise GatewayError(405, 'method_not_allowed', 'use POST /v1/ingest')
                return self._ingest(body)
            if rest == ['status']:
                if method != 'GET':
                    raise GatewayError(405, 'method_not_allowed', 'use GET /v1/ingest/status')
                return self._ingest_status()
            raise GatewayError(404, 'not_found', 'no such route: %s' % path)
        if segments[:2] == ['v1', 'tasks']:
            return self._task_route(method, segments[2:], body)
        if segments[:2] != ['v1', 'events']:
            raise GatewayError(404, 'not_found', 'no such route: %s' % path)
        rest = segments[2:]
        if not rest:
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/events')
            return self._list(query)
        if rest == ['status']:
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/events/status')
            return 200, self._status()
        if rest == ['stats']:
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/events/stats')
            return 200, {'ok': True, 'events': self.store.event_stats(self.clock())}
        if rest == ['consumers']:
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/events/consumers')
            return 200, {'ok': True, 'consumers': self.store.consumers()}
        if rest == ['claim']:
            if method != 'POST':
                raise GatewayError(405, 'method_not_allowed', 'use POST /v1/events/claim')
            return self._claim(self._json_body(body))
        if len(rest) == 1 and rest[0].isdigit():
            event_id = int(rest[0])
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/events/{id}')
            event = self.store.get_event(event_id)
            if event is None:
                raise GatewayError(404, 'not_found', 'event %s does not exist' % event_id)
            return 200, {'ok': True, 'event': event_view(event, detailed=True)}
        if len(rest) == 2 and rest[0].isdigit() and rest[1] in ('ack', 'fail', 'reply', 'skip',
                                                               'say', 'retract'):
            event_id = int(rest[0])
            action = rest[1]
            if method != 'POST':
                raise GatewayError(405, 'method_not_allowed', 'use POST for /v1/events/{id}/%s' % action)
            payload = self._json_body(body)
            handler = {'ack': self._ack, 'fail': self._fail, 'reply': self._reply,
                       'skip': self._skip, 'say': self._say, 'retract': self._retract}[action]
            return handler(event_id, payload)
        raise GatewayError(404, 'not_found', 'no such route: %s' % path)

    def _task_route(self, method, rest, body):
        """任务账本的远程入口：与 CLI ``task list/show/approve/reject/note`` 同一批 store 方法。

        远端消费者（Windows）没有 CLI 可调，但它批准活的口径必须和 mac 上一模一样，
        所以这里**不重新实现判断**：``approval_kind`` 由调用方给（与 CLI 同一个开关），
        状态机只走 ``Store.approve_task`` / ``reject_task``。
        """
        if not rest:
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/tasks')
            cards = [task for task in self.store.tasks('open', limit=50)
                     if not task.get('approval_mid')]
            return 200, {'ok': True, 'pending_approval': len(cards), 'tasks': cards}
        if len(rest) == 1:
            if method != 'GET':
                raise GatewayError(405, 'method_not_allowed', 'use GET /v1/tasks/{id}')
            task = self.store.get_task(rest[0])
            if task is None:
                raise GatewayError(404, 'not_found', 'task %s does not exist' % rest[0])
            return 200, {'ok': True, 'task': task}
        if len(rest) == 2 and rest[1] in ('approve', 'reject', 'note'):
            if method != 'POST':
                raise GatewayError(405, 'method_not_allowed',
                                   'use POST /v1/tasks/{id}/%s' % rest[1])
            payload = self._json_body(body)
            task_id, action = rest[0], rest[1]
            text = _str_field(payload, 'body')
            if action == 'note':
                task = self.store.note_task(task_id, text, self.clock())
                if task is None:
                    raise GatewayError(404, 'not_found', 'task %s does not exist' % task_id)
                return 200, {'ok': True, 'action': 'note', 'task': task}
            mid = _str_field(payload, 'mid')
            if action == 'approve':
                kind = _str_field(payload, 'kind')
                from .store import Store as _Store
                if kind not in _Store.APPROVAL_KINDS:
                    raise GatewayError(400, 'bad_request',
                                       'kind must be one of: ' + ', '.join(_Store.APPROVAL_KINDS))
                result = self.store.approve_task(task_id, approval_mid=mid, approval_kind=kind,
                                                 hints_text=text, now=self.clock())
            else:
                result = self.store.reject_task(task_id, approval_mid=mid, now=self.clock())
            if result.get('task') is None:
                raise GatewayError(404, 'not_found', 'task %s does not exist' % task_id)
            if not result.get('ok') and not result.get('duplicate'):
                raise GatewayError(409, 'conflict',
                                   'task %s is no longer waiting for approval' % task_id)
            return 200, {'ok': True, 'action': action, 'applied': result.get('applied'),
                         'duplicate': result.get('duplicate'), 'task': result.get('task')}
        raise GatewayError(404, 'not_found', 'no such task route')

    # -- handlers -----------------------------------------------------------
    def _list(self, query):
        states = None
        if query.get('state'):
            states = [state.strip() for state in query['state'][0].split(',') if state.strip()]
            unknown = [state for state in states if state not in Store.EVENT_STATES]
            if unknown:
                raise GatewayError(400, 'bad_request', 'unknown event state: ' + ','.join(unknown))
        consumer = query.get('consumer', [None])[0] or None
        try:
            limit = int(query.get('limit', ['50'])[0])
        except ValueError:
            raise GatewayError(400, 'bad_request', 'limit must be an integer')
        if not 1 <= limit <= 200:
            raise GatewayError(400, 'bad_request', 'limit must be 1..200')
        events = self.store.list_events(consumer=consumer, states=states, limit=limit)
        return 200, {'ok': True, 'count': len(events), 'events': [event_view(event, detailed=True) for event in events]}

    def _status(self):
        return {'ok': True, 'external': _read_external(self.home),
                'events': self.store.event_stats(self.clock()),
                'consumers': self.store.consumers(),
                'database': str(Path(self.home) / 'state.sqlite3'), 'db_present': True}

    def _claim(self, payload):
        consumer = _str_field(payload, 'consumer')
        limit = _int_field(payload, 'limit', 5, 1, 50)
        lease = _int_field(payload, 'lease_seconds', 300, 1, 86400)
        kind = payload.get('kind')
        if kind is not None and kind not in Store.EVENT_KINDS:
            raise GatewayError(400, 'bad_request',
                               'kind must be one of: ' + ', '.join(Store.EVENT_KINDS))
        now = self.clock()
        try:
            claimed = self.store.claim_events(consumer, now, limit=limit, lease_seconds=lease, kind=kind)
        except LookupError:
            raise GatewayError(409, 'consumer_not_registered',
                               'consumer %s is not registered; run events register first' % consumer)
        except ValueError as error:
            raise GatewayError(400, 'bad_request', str(error))
        # 与 CLI 的 claim 同一套语义：每条事件按自己的正文检索教材（不共用一块全量），
        # 并把"这一轮注入了哪几条"落到事件行上（归因要用 claim 那一刻的事实）。
        views = []
        for event in claimed:
            block = self._teachings_for(event)
            if block.get('enabled'):
                self.store.set_event_used_ids(event['id'], block.get('used_ids') or [])
            views.append(event_view(event, with_token=True, teachings=block))
        return 200, {'ok': True, 'consumer': consumer, 'now': now, 'count': len(claimed),
                     'events': views}

    def _teachings_for(self, event):
        payload = parse_payload(event['payload'])
        text = payload.get('text') if isinstance(payload, dict) else ''
        if not isinstance(text, str):
            text = ''
        return memory.retrieve(self.home, text)

    def _ack(self, event_id, payload):
        consumer = _str_field(payload, 'consumer')
        claim_token = _str_field(payload, 'claim_token')
        ok = self.store.ack_event(consumer, event_id, self.clock(), claim_token=claim_token)
        if not ok:
            raise GatewayError(409, 'conflict',
                               'ack rejected: event %s is not leased by %s (attention only, lease may have expired)'
                               % (event_id, consumer))
        return 200, {'ok': True, 'consumer': consumer, 'event': event_id}

    def _fail(self, event_id, payload):
        consumer = _str_field(payload, 'consumer')
        claim_token = _str_field(payload, 'claim_token')
        error = _str_field(payload, 'error', required=False) or 'External consumer failed'
        retry_delay = _int_field(payload, 'retry_delay', 30, 0, 86400)
        permanent = bool(payload.get('permanent'))
        state = self.store.fail_event(consumer, event_id, self.clock(), error,
                                      retry_delay=retry_delay, permanent=permanent, claim_token=claim_token)
        if not state:
            raise GatewayError(409, 'conflict',
                               'fail rejected: event %s is not leased by %s' % (event_id, consumer))
        return 200, {'ok': True, 'consumer': consumer, 'event': event_id, 'state': state}

    def _reply(self, event_id, payload):
        from .reply import owner_name_at, render_reply
        from . import hold as hold_switch
        if hold_switch.is_active(self.home):
            # 挂起期间 sender 不工作：此时收下回复只会让事件变 done、消息被丢弃，消费者还以为送达了。
            raise GatewayError(409, 'held', 'bridge is held (mode=held); retry after release')
        consumer = _str_field(payload, 'consumer')
        claim_token = _str_field(payload, 'claim_token')
        text = _str_field(payload, 'body')
        event = self.store.get_event(event_id)
        if event is None:
            raise GatewayError(404, 'not_found', 'event %s does not exist' % event_id)
        if event['kind'] != 'message':
            raise GatewayError(409, 'conflict', 'event %s is %s, only message events can be replied'
                               % (event_id, event['kind']))
        event_payload = parse_payload(event['payload'])
        mid = event_payload.get('message_id') if isinstance(event_payload, dict) else None
        if not isinstance(mid, str) or not mid:
            raise GatewayError(409, 'conflict', 'event %s payload has no message_id' % event_id)
        config = _read_config(self.home)
        # 网关是独立进程（`events serve`），没有进程内的 identity：按同一份实现读 status.json。
        chat_type = event_payload.get('chat_type') if isinstance(event_payload, dict) else None
        mention = (event_payload.get('sender_id')
                   if chat_type == 'group' and isinstance(event_payload, dict) else None)
        body = render_reply(text, config.get('signature'), owner=owner_name_at(self.home),
                            mention=mention, self_open_id=config.get('target_open_id'))
        result = self.store.complete_external_reply(
            consumer, event_id, self.clock(), body, reply_uuid(mid), claim_token=claim_token,
            app_daily_cap=config.get('app_daily_reply_cap', 20))
        if not result.get('ok'):
            reason = result.get('reason', 'reply rejected')
            status = 409 if 'leased' in reason or 'conflict' in reason or 'budget' in reason else 400
            raise GatewayError(status, 'conflict' if status == 409 else 'bad_request', reason)
        return 200, {'ok': True, 'duplicate': bool(result.get('duplicate')), 'consumer': consumer,
                     'event': event_id, 'message_id': mid,
                     'outbox_state': result.get('outbox_state'), 'body_length': len(body)}


    def _skip(self, event_id, payload):
        """「这条不用回」：与 CLI 的 ``events skip`` 同一条实现，远程消费者语义一致。"""
        consumer = _str_field(payload, 'consumer')
        claim_token = _str_field(payload, 'claim_token')
        reason = _str_field(payload, 'reason')
        result = self.store.complete_external_skip(consumer, event_id, self.clock(), reason,
                                                   claim_token=claim_token)
        if not result.get('ok'):
            reason_text = result.get('reason', 'skip rejected')
            status = 409 if 'leased' in reason_text or 'conflict' in reason_text else 400
            raise GatewayError(status, 'conflict' if status == 409 else 'bad_request', reason_text)
        return 200, {'ok': True, 'duplicate': bool(result.get('duplicate')), 'consumer': consumer,
                     'event': event_id, 'message_id': result.get('message_id'), 'reason': reason}

    def _say(self, event_id, payload):
        """补一句说明：与 CLI 的 ``events say --event`` 同一条实现（kind='say'）。

        不改 inbox 状态、不结束任务；幂等键默认由「事件里的消息 + 正文」派生，
        同一条重放不会在群里多发一句。
        """
        from . import hold as hold_switch
        if hold_switch.is_active(self.home):
            raise GatewayError(409, 'held', 'bridge is held (mode=held); retry after release')
        consumer = _str_field(payload, 'consumer')
        claim_token = _str_field(payload, 'claim_token')
        text = _str_field(payload, 'body')
        event = self.store.get_event(event_id)
        if event is None:
            raise GatewayError(404, 'not_found', 'event %s does not exist' % event_id)
        if event['kind'] != 'message':
            raise GatewayError(409, 'conflict', 'event %s is %s, only message events can be said to'
                               % (event_id, event['kind']))
        event_payload = parse_payload(event['payload'])
        mid = event_payload.get('message_id') if isinstance(event_payload, dict) else None
        if not isinstance(mid, str) or not mid:
            raise GatewayError(409, 'conflict', 'event %s payload has no message_id' % event_id)
        config = _read_config(self.home)
        from .reply import owner_name_at, render_reply
        body = render_reply(text, config.get('signature'), owner=owner_name_at(self.home),
                            self_open_id=config.get('target_open_id'))
        uid = payload.get('uuid') if isinstance(payload.get('uuid'), str) and payload.get('uuid') else None
        if uid is None:
            uid = _derived_uuid('say:%s:%s' % (mid, hashlib.sha256(body.encode('utf-8')).hexdigest()))
        result = self.store.complete_external_reply(
            consumer, event_id, self.clock(), body, uid, claim_token=claim_token,
            app_daily_cap=config.get('app_daily_reply_cap', 20), kind='say')
        if not result.get('ok'):
            reason = result.get('reason', 'say rejected')
            status = 409 if 'leased' in reason or 'conflict' in reason or 'budget' in reason else 400
            raise GatewayError(status, 'conflict' if status == 409 else 'bad_request', reason)
        return 200, {'ok': True, 'duplicate': bool(result.get('duplicate')), 'consumer': consumer,
                     'event': event_id, 'kind': 'say', 'message_id': mid,
                     'inbox_id': result.get('inbox_id'), 'row_id': result.get('row_id'),
                     'seq': result.get('seq'), 'outbox_state': result.get('outbox_state'),
                     'body_length': len(body)}

    def _retract(self, event_id, payload):
        """撤回本桥发出去的一条消息：与 CLI 的 ``events retract`` 同一条实现（kind='retract'）。"""
        from . import hold as hold_switch
        if hold_switch.is_active(self.home):
            raise GatewayError(409, 'held', 'bridge is held (mode=held); retry after release')
        consumer = _str_field(payload, 'consumer')
        claim_token = _str_field(payload, 'claim_token')
        target = _str_field(payload, 'message_id')
        uid = payload.get('uuid') if isinstance(payload.get('uuid'), str) and payload.get('uuid') else None
        if uid is None:
            uid = _derived_uuid('retract:%s' % target)
        result = self.store.complete_external_reply(
            consumer, event_id, self.clock(), '', uid, claim_token=claim_token,
            kind='retract', target_mid=target)
        if not result.get('ok'):
            reason = result.get('reason', 'retract rejected')
            if result.get('code') == 'retract_target_not_found':
                raise GatewayError(404, 'not_found', reason)
            status = 409 if ('leased' in reason or 'conflict' in reason
                             or 'already been retracted' in reason) else 400
            raise GatewayError(status, 'conflict' if status == 409 else 'bad_request', reason)
        return 200, {'ok': True, 'duplicate': bool(result.get('duplicate')), 'consumer': consumer,
                     'event': event_id, 'kind': 'retract', 'message_id': target,
                     'inbox_id': result.get('inbox_id'), 'row_id': result.get('row_id'),
                     'seq': result.get('seq'), 'outbox_state': result.get('outbox_state')}


def serve(gateway, host, port, ready=None):
    """Serve until KeyboardInterrupt. ``ready`` is called with the bound address."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = 'feishu-bridge-gateway/1'
        protocol_version = 'HTTP/1.1'

        def _handle(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send(413, {'ok': False, 'error': {'code': 'payload_too_large',
                                                        'message': 'bad Content-Length or body too large'}})
                return
            body = self.rfile.read(length) if length else b''
            status, payload = gateway.dispatch(self.command, self.path, dict(self.headers.items()),
                                               body, remote=self.client_address[0])
            self._send(status, payload)

        def _send(self, status, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = _handle
        do_POST = _handle

        def log_message(self, fmt, *args):  # never log bodies or tokens
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    if ready is not None:
        ready(server.server_address)
    try:
        server.serve_forever()
    finally:
        server.server_close()
