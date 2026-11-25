"""``as_bot`` 身份：用**机器人**身份收发消息（长连接收事件 + 机器人 token 发）。

设计见 ``docs/design-identity-as-user-as-bot.md`` §1/§2/§5：

* 与 ``as_user`` **功能相同**，差别只在"谁在说话"；
* 收消息走 **Node sidecar 长连接**（不需要公网入口），sidecar 把事件 POST 到本机回环
  ``/v1/ingest``，桥按与 ``as_user`` **完全相同的下游**处理（inbox → details → events）；
* 断线期间的缺口由 :meth:`BotIdentity.collect` 按时间窗对账补回（不猜、不丢）；
* 一台机器只有一种身份：本类与 ``UserIdentity`` 不共存（工厂负责拒绝）。
"""
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .base import TransportError
from .user import UserIdentity, mention_ids, mentioned

LOGGER = logging.getLogger(__name__)

#: 机器人身份需要的最小 scope（缺哪个就在错误里点名，不静默降级）
BOT_SCOPES = frozenset({
    'im:message',
    'im:message:send_as_bot',
    'im:chat:readonly',
})


class BotIdentity(UserIdentity):
    """机器人身份。

    继承 ``UserIdentity`` 只为了复用 `lark-cli` 的参数构造、错误分类与回读校验；
    **凡是"以谁的名义"的地方都覆写**：``--as bot``、``is_mentioned``、``inventory``、
    ``collect``、``start_channel``。
    """

    mode = 'bot'
    api_identity = 'bot'
    #: 机器人不会"自己说话"，所以没有"别回自己的话"这条守卫（差异做成显式属性）
    needs_self_guard = False

    def __init__(self, config, stop=None):
        super().__init__(config, stop if stop is not None else threading.Event())
        self.bot_block = (((config or {}).get('identity') or {}).get('bot') or {})
        self.app_id = self.bot_block.get('app_id')
        if not self.app_id:
            raise RuntimeError('identity.mode=bot 需要 identity.bot.app_id')
        self.bot_open_id = self.bot_block.get('bot_open_id') or None
        self.sidecar_command = self.bot_block.get('sidecar_command') or None
        #: 长连接由谁托管：``inproc``（桥进程内拉起，开发/测试用）或 ``service``
        #: （install.py 装成独立单元，systemd/launchd 托管）。二选一，**不许两边都起**——
        #: 两个长连接会重复投递事件，等于本机双答自己。
        self.sidecar_mode = str(self.bot_block.get('sidecar') or 'inproc').strip().lower()
        if self.sidecar_mode not in ('inproc', 'service'):
            raise RuntimeError("identity.bot.sidecar 只能是 'inproc' 或 'service'，当前是 %r"
                               % (self.bot_block.get('sidecar'),))
        self.ingest_url = (self.bot_block.get('ingest_url')
                           or 'http://127.0.0.1:%s/v1/ingest' % (self.bot_block.get('gateway_port') or 8787))
        self._sidecar = None
        self._sidecar_thread = None

    # -- 身份 ---------------------------------------------------------------
    def _auth_bot(self):
        """`auth status` 里的 bot 段；读不出就是空 dict（永不抛，让调用方决定怎么失败）。"""
        try:
            result = self.run(['auth', 'status', '--json'])
        except (TransportError, KeyError) as error:  # 未配置 lark_binary / CLI 不通：当成"读不出"
            LOGGER.debug('bot auth probe unavailable: %s', error)
            return {}
        identities = result.get('identities')
        bot = identities.get('bot') if isinstance(identities, dict) else None
        return bot if isinstance(bot, dict) else {}

    def identity(self):
        bot = self._auth_bot()
        if not bot.get('available'):
            raise TransportError('Expected Feishu bot is not authenticated '
                                 '(run: lark-cli config init / auth status)', 'authorization',
                                 retryable=False)
        if self.app_id and bot.get('appId') and bot.get('appId') != self.app_id:
            raise TransportError('Configured app_id %s does not match authenticated bot %s'
                                 % (self.app_id, bot.get('appId')), 'authorization', retryable=False)
        missing = self.bot_scope_gap()
        if missing:
            raise TransportError('Missing bot scopes: ' + ', '.join(sorted(missing)),
                                 'authorization', retryable=False)
        info = {key: bot.get(key) for key in ('appId', 'available', 'tokenStatus')}
        # 给人看的自称：as_bot 没有 userName，回执/体检里用这个代替
        info['label'] = '应用 %s 的机器人' % (bot.get('appId') or '未知应用')
        return info

    def bot_scope_gap(self):
        """缺哪些 scope。读不出 scope 时**不拦**（读不到 ≠ 没有），交由真实调用暴露。"""
        scope = self._auth_bot().get('scope')
        if isinstance(scope, list):
            scope = ' '.join(item for item in scope if isinstance(item, str))
        granted = set(scope.split()) if isinstance(scope, str) else None
        if not granted:
            return set()
        return BOT_SCOPES - granted

    def user_scopes(self):
        """机器人身份没有"用户 token 的 scope"，教学通道那条前置判断因此判为缺。"""
        return set()

    def self_chat_id(self):
        raise TransportError('as_bot 没有"自己跟自己"的会话；命令通道是"你与机器人的私聊"',
                             'protocol', retryable=False)

    def is_mentioned(self, message):
        if message.get('via') == 'ingest':
            # 长连接只在"机器人被 @"时投递群消息，不用再猜一遍
            return True
        if self.bot_open_id:
            return self.bot_open_id in mention_ids(message)
        # 没配 bot_open_id 时退回"有任何人被 @"（保守：宁可多处理一条，也不要漏）
        return bool(mention_ids(message))

    def is_owner(self, sender_id):
        return bool(sender_id) and sender_id in set(self.owner_open_ids())

    # -- 通道与自证（与 as_user 语义相同、实现不同） -----------------------
    def self_sender_id(self):
        """机器人发的消息，``sender.id`` 是 **app_id**（不是 open_id）。"""
        return self.app_id

    def command_chat_ok(self, chat_id, *, self_chat_ids=(), chat_type=None, known_group=False):
        """机器人身份：命令只在"主人与机器人的私聊"里生效。

        判断依据是会话类型：``p2p`` 才算；群聊一律不执行（与 as_user 的"群里命令不执行"同一语义）。
        没有 ``chat_type`` 时退回"不在群册里就算私聊"（与采集侧的既有口径一致）。
        """
        if chat_type == 'group':
            return False
        if chat_type == 'p2p':
            return True
        return not known_group

    def control_chat_ids(self, config, teach_chat=None):
        """机器人有长连接推 DM，不需要轮询控制指令。"""
        return set()

    # -- 收：长连接 sidecar --------------------------------------------------
    def sidecar_argv(self):
        if isinstance(self.sidecar_command, list) and self.sidecar_command:
            return list(self.sidecar_command)
        default = Path(__file__).resolve().parents[2] / 'sidecar' / 'index.js'
        return ['node', str(default)]

    def sidecar_env(self):
        env = dict(os.environ)
        env.update({
            'FEISHU_APP_ID': str(self.app_id),
            'FEISHU_APP_SECRET': str(self.bot_block.get('app_secret') or ''),
            'FEISHU_INGEST_URL': self.ingest_url,
            'FEISHU_INGEST_TOKEN_FILE': str(self.bot_block.get('ingest_token_file')
                                            or (Path(self.config.get('home') or '.') / 'gateway-token')),
            'FEISHU_LOG_LEVEL': str(self.bot_block.get('log_level') or 'info'),
        })
        return env

    def start_channel(self, on_message=None):
        """起 sidecar 长连接并守护它（崩了重连，``stop_channel`` 时退出）。

        为什么用守护线程而不是直接 `Popen` 一次：长连接断线是常态（网络切换、飞书侧滚动重启），
        sidecar 自己会重连；但如果进程本身退出，需要有人把它拉起来——这与 service 的
        "worker 必须活着"是同一个判据。
        """
        if self.sidecar_mode == 'service':
            # 由 systemd/launchd 托管的 sidecar 已经连着飞书；桥再拉一个就是两条长连接。
            LOGGER.info('sidecar is managed by the service manager; bridge will not spawn it')
            return None
        if self._sidecar_thread is not None:
            return self._sidecar_thread
        argv = self.sidecar_argv()

        def supervise():
            backoff = 1.0
            while not self.stop.is_set():
                try:
                    LOGGER.info('starting sidecar: %s', ' '.join(argv[:2]))
                    self._sidecar = subprocess.Popen(argv, env=self.sidecar_env(),
                                                     stdout=subprocess.PIPE,
                                                     stderr=subprocess.STDOUT, text=True)
                    for line in self._sidecar.stdout:
                        if line.strip():
                            LOGGER.info('sidecar: %s', line.rstrip()[:400])
                        if self.stop.is_set():
                            break
                    self._sidecar.wait(timeout=10)
                except (OSError, subprocess.SubprocessError) as error:
                    LOGGER.warning('sidecar failed: %s', error)
                finally:
                    if self._sidecar and self._sidecar.poll() is None:
                        self._sidecar.terminate()
                if self.stop.is_set():
                    break
                LOGGER.warning('sidecar exited; restarting in %.0fs', backoff)
                self.stop.wait(backoff)
                backoff = min(30.0, backoff * 2)

        self._sidecar_thread = threading.Thread(target=supervise, name='sidecar', daemon=True)
        self._sidecar_thread.start()
        return self._sidecar_thread

    def stop_channel(self):
        process = self._sidecar
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                process.kill()
        thread = self._sidecar_thread
        if thread is not None:
            thread.join(timeout=10)

    # -- 对账：断线窗口按时间窗补 --------------------------------------------
    def collect(self, scan_name, start_ms, end_ms):
        """机器人身份的"对账采集"：按窗口读它所在会话的消息，挑出 @ 机器人的那些。

        `as_user` 用搜索（服务端帮我们筛 ``is_at_me``）；机器人没有"@我"的搜索接口，
        只能按会话读再本地筛——所以这里**必须**有会话清单（:meth:`inventory`），
        且只读窗口内的消息（与扫描窗口同一套水位语义）。
        """
        chats = [row.get('chat_id') for row in self.inventory() if row.get('chat_id')]
        ids, seen = [], set()
        for chat_id in chats:
            if self.stop.is_set():
                break
            try:
                messages, _complete = self.chat_messages(chat_id, start_ms, end_ms)
            except TransportError as error:
                LOGGER.warning('bot reconcile skip %s: %s', chat_id, error)
                continue
            for message in messages:
                mid = message.get('message_id')
                if not isinstance(mid, str) or not mid.startswith('om_') or mid in seen:
                    continue
                if not self.is_mentioned(message):
                    continue
                seen.add(mid)
                ids.append(mid)
        LOGGER.info('bot reconcile %s window: %d candidate(s)', scan_name, len(ids))
        return {'ids': ids, 'next_token': None, 'notice': None}

    def inventory(self):
        """机器人所在的会话清单（`im +chat-list --as bot`）。"""
        rows, token, seen = [], None, set()
        while True:
            args = ['im', '+chat-list', '--as', 'bot', '--page-size', '100',
                    '--sort', 'create_time', '--format', 'json']
            if token:
                args += ['--page-token', token]
            data = self.run(args).get('data')
            if not isinstance(data, dict) or not isinstance(data.get('chats'), list):
                raise TransportError('Unrecognized bot chat inventory response', 'protocol')
            for chat in data['chats']:
                chat_id = chat.get('chat_id') if isinstance(chat, dict) else None
                if isinstance(chat_id, str) and chat_id and chat_id not in seen:
                    seen.add(chat_id)
                    rows.append(chat)
            token = data.get('page_token') if data.get('has_more') else None
            if not token:
                return rows

    def search(self, start_ms, end_ms, page_token=None, chat_id=None):
        raise TransportError('as_bot 不做搜索采集：群消息由长连接投递，缺口走 collect() 对账',
                             'protocol', retryable=False)

    # -- 其它 ---------------------------------------------------------------
    def whoami(self):
        return {'mode': self.mode, 'app_id': self.app_id,
                'display': self.bot_block.get('display') or self.config.get('signature') or '',
                'owner_open_ids': list(self.owner_open_ids())}

    def describe(self):
        app_id = str(self.app_id or '')
        masked = (app_id[:6] + '…' + app_id[-4:]) if len(app_id) >= 12 else ('已配置(短)' if app_id else '未配置')
        open_id_ok = bool(self.bot_open_id)
        gap = self.bot_scope_gap()
        return [('identity', 'mode=bot app_id=%s' % masked, bool(app_id)),
                ('identity_channel', 'ws(sidecar=%s → /v1/ingest)' % self.sidecar_mode, bool(app_id)),
                # 没配 bot_open_id 时 @ 判定退化成"有任何人被 @"（偏保守：可能多处理一条）
                ('bot_open_id', (self.bot_open_id or '未配置（@ 判定放宽）'), True),
                ('bot_scopes', ('ok' if not gap else '缺: ' + ','.join(sorted(gap))), not gap)]
