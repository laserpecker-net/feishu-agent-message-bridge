import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import signal
import threading
import time

from .reply import owner_name_at, render_reply
from .store import Store, reply_uuid
from . import commands as command_table
from . import help as help_manual
from . import hold
from . import teach as teach_log
from .identity import build_identity
from .transport import (Transport, TransportError, mentioned, message_text, message_version,
                        verify_reply_reason)

#: Worker 线程名 = 「进程起来之后应该活着的东西」的**唯一**答案。
#: 验收脚本（`dist/wsl2/wsl/verify.sh`）从这一行读，而不是自己抄一份名单：抄一份就会在
#: worker 增减时让「旧包验新代码」假红（2026-11-05 真机：旧 verify.sh 断言 4 个、代码只有 3 个）。
WORKER_NAMES = ('collector', 'details', 'sender')

# P1-02/P1-03: the collector must be able to recover its search window and must never
# pin itself to a poisoned cursor forever.
WINDOW_DEFAULT_MS = 86400000
WINDOW_FLOOR_MS = 600000
#: 飞书搜索的时间区间只到**秒**（`transport.search` 用 ISO 秒；真机 2026-11-06：带毫秒会被
#: 直接拒）。所以区间短于 1 秒时，两端截断成同一个时刻，请求必被拒——而"拒了但边界看着合法"
#: 就是死循环：每一拍拿同一对边界问、每一拍被拒，消息一条也搜不到。
MIN_SCAN_SPAN_MS = 1000
WINDOW_GROWTH_SUCCESSES = 5
MAX_SCAN_FAILURES = 5


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)


def _release_from(path):
    parts = list(Path(path).resolve().parts)
    for index in range(len(parts) - 2):
        if parts[index] == 'releases' and parts[index + 2] == 'bridge':
            return parts[index + 1]
    return None


# Resolved once at import: the launcher imports the package through the `current` symlink,
# so resolving `__file__` on every call would report a *newly installed* release while the
# process still runs the old code — `status.update_pending` would then never fire.
_LOADED_RELEASE = _release_from(__file__)


def running_release(path=None):
    """P2-22: identify the release directory this module was installed from, if any."""
    if path is None:
        return _LOADED_RELEASE
    return _release_from(path)


def _usable_bounds(scan):
    """游标能不能直接拿去问：至少 1 秒宽（否则截到秒后两端同一时刻，飞书必拒）。"""
    start, end = scan.get('start_ms'), scan.get('end_ms')
    return (isinstance(start, int) and isinstance(end, int)
            and end - start >= MIN_SCAN_SPAN_MS)


def scan_bounds(epoch, position, target, width, origin=None):
    floor = max(epoch, origin or epoch)
    start = max(floor, position - min(300000, width // 2))
    # P2-11: a backward wall-clock jump can put the watermark ahead of "now"; without this
    # clamp begin_scan would receive end < start and fail on every single tick.
    # 但回拨幅度不得超过 epoch：挂起/首次激活的时间下界是硬边界，扫描不许越过它去
    # 回补关闭窗口（否则 release 之后仍会把下班期间的消息捞回来）。
    if start >= target:
        start = max(floor, target - max(1000, width // 2))
    end = min(target, start + width)
    if end <= start:
        end = start + 1000
    return start, end


def self_chat_ids(config):
    """Chats where the owner's own @-mentions are treated as commands (opt-in).

    Only explicit `oc_...` chat IDs are accepted; anything else is ignored so a typo
    cannot silently turn on self-message handling for every chat.
    """
    value = config.get('self_chat_ids') if isinstance(config, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return set()
    return {item for item in value if isinstance(item, str) and item.startswith('oc_')}


class Service:
    def __init__(self, home, config):
        self.home, self.config = Path(home), dict(config)
        self.stop = threading.Event()
        self.authenticated = threading.Event()
        self.store = Store(self.home / 'state.sqlite3')
        # 身份由工厂按 config.identity.mode 实例化（一台机器一种身份，见设计稿 §5）
        self.transport = build_identity(self.config, self.stop)
        self.identity = {}
        self.health_lock = threading.Lock()
        self.health = {'started_at': time.time(), 'last_search_at': 0, 'last_inventory_at': 0, 'errors': {}}
        self.threads = []
        self._external = {}
        self._external_key = None
        # 挂起开关：状态文件按 mtime+size 缓存；控制通道每 30s 轮询一次自聊会话。
        self._hold = {}
        self._hold_key = None
        self._was_held = False
        self._control_last = 0.0
        self._control_seen = {}
        self._control_order = []
        self._control_lock = threading.Lock()
        # 命令引导语的节流：同一个私聊里最短 5 分钟提醒一次，不刷屏。
        self._guide_at = {}
        # 私聊通道：没写过 teach.json 就默认开通（会话自己认），所以要记住上次尝试的时间。
        self._self_channel_try_at = 0.0
        self._p2p_ok = None
        self._p2p_checked_at = 0.0

    def error(self, stage, error):
        message = '%s: %s' % (type(error).__name__, str(error)[:700])
        logging.warning('%s %s', stage, message)
        with self.health_lock:
            self.health['errors'][stage] = {'at': time.time(), 'message': message}
        if isinstance(error, TransportError) and error.kind == 'authorization':
            self.authenticated.clear()

    def success(self, stage, timestamp=None):
        with self.health_lock:
            self.health['errors'].pop(stage, None)
            if timestamp:
                self.health[timestamp] = time.time()

    def enabled(self):
        try:
            current = json.loads((self.home / 'config.json').read_text())
            return bool(current.get('sending_enabled')) and current.get('target_open_id') == self.config['target_open_id']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # P2-10: a broken/torn config must never silently kill a worker thread.
            return False

    # --- 挂起开关（hold / release）------------------------------------------
    # 语义与验收见 docs/bridge-hold-switch-requirements.md。要点：挂起期间零采集、零入队、
    # 零回复；恢复时把 service_epoch_ms 前移到恢复时刻，让关闭窗口永久不参与回溯。
    def hold_state(self):
        """读取 home/hold.json（mtime+size 缓存）。缺失或损坏时 fail-open。"""
        path = self.home / hold.HOLD_FILE
        try:
            stat = path.stat()
        except OSError:
            self._hold, self._hold_key = {'active': False}, None
            return self._hold
        key = (stat.st_mtime_ns, stat.st_size)
        if self._hold_key != key:
            try:
                loaded = json.loads(path.read_text())
            except (OSError, ValueError):
                loaded = None
            if not isinstance(loaded, dict):
                logging.warning('hold state unreadable; failing open (bridge keeps running)')
                loaded = {'active': False}
            self._hold, self._hold_key = loaded, key
        return self._hold

    def held(self):
        return bool(self.hold_state().get('active'))

    def sync_hold(self, now):
        """应用挂起状态迁移：进入→作废全部积压；退出→前移时间下界并清残留。"""
        state = self.hold_state()
        if state.get('active'):
            since = hold._as_int(state.get('since_ms')) or int(now * 1000)
            if since > (self.store.meta('hold_entry_applied_ms') or 0):
                dropped = self.store.drop_pending(now, error='Bridge held')
                total = (self.store.meta('hold_dropped_total') or 0) + dropped
                self.store.set_meta('hold_dropped_total', total)
                self.store.set_meta('hold_dropped_current', dropped)
                self.store.set_meta('hold_entry_applied_ms', since)
                self.store.set_meta('hold_since_ms', since)
                # 指令游标从挂起时刻开始，重启后不会把历史里的"上班"捞出来误恢复。
                self.store.set_meta('control_cursor_ms', max(int(now * 1000), since))
                logging.info('hold engaged since=%s dropped=%s', hold.clock(since), dropped)
                self._hold = dict(state)
            return
        release = hold._as_int(state.get('last_release_at_ms')) or 0
        if release > (self.store.meta('hold_release_applied_ms') or 0):
            dropped = self.store.drop_pending(now, error='Bridge held')
            total = (self.store.meta('hold_dropped_total') or 0) + dropped
            self.store.set_meta('hold_dropped_total', total)
            self.store.set_meta('hold_dropped_current',
                                (self.store.meta('hold_dropped_current') or 0) + dropped)
            self.store.apply_release_floor(release)
            self.store.set_meta('hold_release_applied_ms', release)
            logging.info('hold released at=%s dropped_extra=%s', hold.clock(release), dropped)

    def enter_hold(self, by, now, reason=None):
        state = hold.read_state(self.home)
        if state.get('active'):
            return '已在挂起中（自 %s）。' % hold.clock(state.get('since_ms'))
        payload = dict(hold.default_state())
        payload.update({'active': True, 'since_ms': int(now * 1000), 'by': by, 'reason': reason})
        hold.write_state(self.home, payload)
        self._hold_key = None
        self.sync_hold(now)
        return hold.hold_notice(payload)

    def release_hold(self, by, now):
        state = hold.read_state(self.home)
        if not state.get('active'):
            return '未处于挂起状态。'
        state.update({'active': False, 'last_release_at_ms': int(now * 1000), 'released_by': by})
        hold.write_state(self.home, state)
        self._hold_key = None
        self.sync_hold(now)
        dropped = self.store.meta('hold_dropped_current') or 0
        return hold.release_notice(dropped, since=state.get('since_ms'))

    def control_tick(self, now):
        """轮询自聊群里的 下班/上班（个人账号收不到推送事件，只能轮询）。

        教材私聊不在这里：它走正常采集管道（`transport.search(chat_id=…)` → inbox → details），
        教材/指令/派活都在 details 的分流点里判（见 `route_self_chat`）。只留一个入口。
        """
        chats = sorted(self_chat_ids(self.config))
        if not chats:
            return
        interval = self.config_seconds('hold_control_poll_seconds', 30)
        if now - self._control_last < interval:
            return
        self._control_last = now
        end_ms = int(now * 1000)
        cursor = self.store.meta('control_cursor_ms') or 0
        start_ms = max(int(cursor), end_ms - 300000)
        failed = False
        advanced = end_ms
        for chat in chats:
            try:
                messages, complete = self.transport.chat_messages(chat, start_ms, end_ms)
            except Exception as error:
                failed = True
                self.error('control', error)
                continue
            for message in messages:
                try:
                    self.handle_control_message(message, now)
                except Exception as error:
                    self.error('control', error)
            if not complete:
                # 只读到一部分：游标停在已读到的位置，下一拍从这里继续，不能推到窗口末尾。
                read_to = max([int(message.get('create_time') or 0) for message in messages] or [0])
                advanced = min(advanced, read_to or start_ms)
                logging.warning('control window incomplete chat=%s read=%d', chat, len(messages))
        if not failed:
            self.store.set_meta('control_cursor_ms', advanced)
            self.success('control')

    def send_manual(self, chat_id, mid, now):
        """把说明书按序发出去：同一消息 id 一组 say，``seq`` 递增，重放不重发。

        走 ``store.announce``——与批准回执同一条发送路径（出口仍然唯一），
        只是它不要求"注册成消费者"：桥自己说一句说明书，不该在 consumer 列表里多一个幽灵。
        """
        pieces = help_manual.chunks()
        for index, body in enumerate(pieces, 1):
            self.store.announce(now, body, inbox_id=mid, uuid='help:%s:%d' % (mid, index))
        logging.info('help manual queued mid=%s chunks=%d', mid, len(pieces))

    #: 「命令要精确」的提醒在同一个私聊里的最短间隔（秒）。
    GUIDE_INTERVAL = 300.0

    def _guide_ready(self, chat_id, now):
        last = self._guide_at.get(chat_id) or 0
        if now - last < self.GUIDE_INTERVAL:
            return False
        self._guide_at[chat_id] = now
        return True

    def status_line(self, now):
        """「状态」命令的回执：只说主人能看懂的三件事。"""
        state = hold.read_state(self.home)
        if state.get('active'):
            head = '我已下班（自 %s）。' % hold.clock(state.get('since_ms'))
        else:
            head = '我在正常接待。'
        queue = self.store.approval_queue()
        if queue:
            listing = '\n'.join(
                '  %d. %s（等你 %d 分钟）' % (
                    index, (item.get('title') or '')[:40],
                    max(0, int((now - item.get('created_at') or now) / 60)))
                for index, item in enumerate(queue, 1))
            pending = '- 等你批的卡（%d 张）：\n%s' % (len(queue), listing)
        else:
            pending = '- 等你批的卡：0 张'
        blocked = self.store.blocked_queue()
        if blocked:
            listing = '\n'.join(
                '  %d. %s' % (index, (item.get('title') or '')[:40])
                for index, item in enumerate(blocked, 1))
            pending += '\n- 卡住的活（%d 张，回「执行」重派）：\n%s' % (len(blocked), listing)
        last = self.health.get('last_search_at') or 0
        age = '%d 分钟前' % max(0, int((now - last) / 60)) if last else '还没扫过'
        return '%s\n%s\n- 最近一次扫描：%s' % (head, pending, age)

    def _max_attempts(self):
        """重派上限（``worker.max_attempts``，默认 3）：到顶就把活标成没做成，不反复撞墙。"""
        block = (self.config or {}).get('worker') or {}
        try:
            return max(1, int(block.get('max_attempts') or 3))
        except (TypeError, ValueError):
            return 3

    def _pending_listing(self, queue, head):
        """待批卡片的编号清单——``状态`` 与 ``执行`` 用同一份，编号才不会打架。"""
        lines = [head]
        for index, item in enumerate(queue, 1):
            lines.append('%d. %s' % (index, (item.get('title') or '')[:40]))
        return '\n'.join(lines)

    def _run_execute(self, payload, mid, now, text):
        """``执行``：批准一张等你点头的卡（编号与 ``状态`` 同源）。

        三种情况分开回答，不许糊成一句：

        * 没有待批：直接说"没有等你批的卡"；
        * 有待批但没指定第几张（且不止一张）：列编号，让他回「执行 2」；
        * 指定了：越界就说现在只有几张，命中就批准（``approval_kind='command'``）。
        """
        queue = self.store.approval_queue()
        if not queue:
            # 没有待批的卡 → 看有没有**卡住的活**（blocked）：那条路的出口也是「执行」。
            # 不新造「继续」这个命令：命令词每多一个，说明书、测试、主人的记忆都要跟着长。
            blocked = self.store.blocked_queue()
            if not blocked:
                self.store.announce(now, '现在没有等你批的卡，也没有卡住的活。',
                                    inbox_id=mid, uuid='cmd:%s:exec:none' % mid)
                return
            if payload is None and len(blocked) > 1:
                self.store.announce(now, self._pending_listing(
                    blocked, '现在有 %d 张卡住的活，要指定第几张：' % len(blocked)) + '\n发「执行 2」这样。',
                    inbox_id=mid, uuid='cmd:%s:exec:blocked:none' % mid)
                return
            index = 1 if payload is None else int(payload)
            if index < 1 or index > len(blocked):
                self.store.announce(now, self._pending_listing(
                    blocked, '现在只有 %d 张卡住的活，没有第 %d 张：' % (len(blocked), index)),
                    inbox_id=mid, uuid='cmd:%s:exec:blocked:bad' % mid)
                return
            target = blocked[index - 1]
            result = self.store.redispatch_blocked(target['id'], now=now,
                                                   max_attempts=self._max_attempts())
            title = (target.get('title') or '')[:40]
            if result.get('exhausted'):
                body = '这张已经试满 %d 次还是卡住，我先把它标成没做成：%s' % (
                    self._max_attempts(), title)
            elif result.get('applied'):
                body = '又派了一次：%s（第 %d 次）。' % (title, result.get('attempt') or 2)
            else:
                body = '这张已经不在卡住的状态了：%s' % title
            self.store.announce(now, body, inbox_id=mid,
                                uuid='cmd:%s:exec:blocked:%d' % (mid, index))
            return
        if payload is None and len(queue) > 1:
            self.store.announce(now, self._pending_listing(
                queue, '现在有 %d 张等你批，要指定第几张：' % len(queue)) + '\n发「执行 2」这样。',
                inbox_id=mid, uuid='cmd:%s:exec:none' % mid)
            return
        index = 1 if payload is None else int(payload)
        if index < 1 or index > len(queue):
            self.store.announce(now, self._pending_listing(
                queue, '现在只有 %d 张等你批，没有第 %d 张：' % (len(queue), index)),
                inbox_id=mid, uuid='cmd:%s:exec:bad' % mid)
            return
        target = queue[index - 1]
        result = self.store.approve_task(target['id'], approval_mid=mid, approval_kind='command',
                                         hints_text=(text or '执行'), now=now)
        title = (target.get('title') or '')[:40]
        if result.get('applied'):
            body = '已批准：%s（第 %d 张）。我把它推到"开始干"了。' % (title, index)
        elif result.get('duplicate'):
            body = '这条已经执行过了：%s' % title
        else:
            body = '这张已经不在等你批了：%s' % title
        self.store.announce(now, body, inbox_id=mid, uuid='cmd:%s:exec:%d' % (mid, index))

    def run_command(self, verb, payload, chat_id, mid, now, text=None):
        """执行一条精确命令；回执走 ``store.announce``（出口仍然唯一）。

        ``text`` 是主人原话（``执行 2`` 这类要整段进 hints，账本上不能只有编号）。

        命令是**确定性**的：动词已经由 :func:`bridge.commands.match` 判定，这里只做动作，
        不做任何"猜主人想要什么"的事。失败只记日志并回一句人话，绝不影响 details 线程。
        """
        # 回执挂在触发它的那条消息下（有 inbox 行时）；没有（例如测试直接调命令）就按会话发，
        # 免得 announce 因为找不到挂载点而整条丢掉。
        scope = {'inbox_id': mid} if self.store.message_row(mid) else {'chat_id': chat_id}

        def say(body):
            self.store.announce(now, body, uuid='cmd:%s:%s' % (mid, verb), **scope)

        try:
            if verb == 'help':
                self.send_manual(chat_id, mid, now)
            elif verb in ('hold', 'release'):
                with self._control_lock:
                    seen = mid in self._control_seen
                    self._control_seen[mid] = True
                    self._control_order.append(mid)
                    while len(self._control_order) > 500:
                        self._control_seen.pop(self._control_order.pop(0), None)
                if seen:
                    return  # 控制通道已经回过这条了，别回两遍
                notice = (self.enter_hold('feishu', now) if verb == 'hold'
                          else self.release_hold('feishu', now))
                if notice:
                    say(notice)
            elif verb == 'execute':
                self._run_execute(payload, mid, now, text)
            elif verb == 'status':
                say(self.status_line(now))
            elif verb == 'list':
                active = [entry for entry in teach_log.entries(self.home) if entry.get('status') == 'active']
                if not active:
                    say('现在没有生效的记忆条目。')
                else:
                    say('现在生效的记忆条目：\n' + '\n'.join(
                        '%d. %s' % (index, entry['text']) for index, entry in enumerate(active, 1)))
            elif verb == 'revert':
                entry = teach_log.forget_last(self.home)
                if entry:
                    say('已撤回：%s\n历史留着，以后想找还能找到。' % entry['text'])
                else:
                    say('没有可撤回的记忆条目。')
            elif verb == 'remember':
                result = teach_log.add_from_model(self.home, 'principle', payload, mid,
                                                  at_ms=int(now * 1000))
                entry = result['entry']
                if result.get('duplicate'):
                    say('这条已经记过了：%s' % entry['text'])
                else:
                    say('记下了（%s）：%s\n觉得不对，发「撤回」。' % (entry.get('id'), entry['text']))
        except Exception as error:  # noqa: BLE001 - 一条命令失败不该拖垮 details 线程
            self.error('command', error)
            logging.warning('command failed verb=%s mid=%s: %s', verb, mid, error)

    def route_self_chat(self, message, now):
        """教材私聊里的一条消息怎么处理——**唯一入口**（details 在生成事件之前调用）。

        返回 ``None`` = 已处理（图片退回、引用答复），不该产生事件；
        返回 ``dict`` = 交给事件管道：``{"text":…, "self_intent":"infer"}``。

        2026-11-06 本人第二次拍板：**连命令词、`问：`/`做：`、`原则：` 这些协议信号也不要了**——
        "让用模型去判断是什么指令，而不是协议信号和规则"。所以这里只剩两类结构性判断：

        1. 准入（会话是这个私聊、发送者是本人、@ 了本人、`create_time` 晚于启用时刻、回放去重）
           —— 这不是语言判断，是"这条算不算数、归谁"的账本判断；
        2. 引用回复了我发的那条求助 —— 这是**结构信号**（飞书给了 parent_id），不是语义判断；
           答复必须走这条，否则分不清他在答哪一条（2026-11-05 事故，见 docs/HISTORY.md §24）。

        其余全部 ``self_intent=infer``：由模型判它是教材、派活、提问、吐槽，还是"查清单/撤回/复盘"
        这类操作，并自己动手（写教材用答复 JSON 的 ``teach`` 字段；清单/撤回/复盘用
        ``feishu-bridge teach list|show|revert|reflect``，prompt 里给了这些命令）。
        """
        chat_id = teach_log.enabled_chat(self.home)
        if not chat_id or message.get('chat_id') != chat_id:
            return None
        mid = message.get('message_id')
        text = message_text(message)
        try:
            create_ms = int(message.get('create_time'))
        except (TypeError, ValueError):
            create_ms = int(now * 1000)
        if create_ms < int(teach_log.read_config(self.home).get('enabled_at_ms') or 0):
            return None
        if teach_log.already_handled(self.home, mid):
            # 换成统一入口时第一次扫描会回放窗口：处理过的消息不再回执，也不重复落盘。
            logging.info('teach replay skipped mid=%s', mid)
            return None
        if message.get('msg_type') not in ('text', 'post'):
            self._teach_reply(message, '这条收不了：教材只认文字（图片、文件、语音读不出意思）。'
                                       '把要教的换成文字再发。')
            return None
        if self.try_answer_question(message, now, chat_id):
            return None
        if self.try_task_approval(message, now, chat_id):
            return None
        logging.info('teach infer mid=%s', mid)
        return {'text': teach_log.clean_text(text) or text, 'self_intent': 'infer'}

    def try_answer_question(self, message, now, chat_id):
        """本人**引用回复** bridge 的求助 = 他的答复：落教材、回执、把原来那条消息重新排上。

        只有引用回复才认答复（结构性信号，不猜）；没引用的消息一律交给正常分流，
        既不当答复、也不吞掉。待答求助 30 分钟过期（`teach_log.ASK_TTL_MS`）。

        不能唤醒已经结束的消费进程（它按事件粒度启停），所以答完之后重放一个新事件，
        带 `owner_answer`；同一个问题重放一次（dedupe_key 固定），重复回答是幂等的。
        """
        mid = message.get('message_id')
        if message.get('msg_type') not in ('text', 'post'):
            return False
        parent_id = message.get('parent_id')
        if not parent_id:
            # 没引用 = 不知道它在答哪个问题，就**不当答复**：交给正常分流。
            # 2026-11-05 晚的事故就是在这里吞消息：两条求助挂着时，任何新的私聊消息都被一句
            # 「对不上号」顶回去、既不落教材也不进事件管道——本人发「帮我打开 UU APP」什么也没发生。
            # 判据：私聊里的消息永远要么被处理、要么被明确回执，绝不允许"当成答复"然后消失。
            return False
        # 引用了某条消息：只认「引用的是我正在等的那条求助」。引用别的消息说明他在说别的事，
        # 不能顺手当成答案（猜错了会写进一条错的教材，而且会把原消息答歪）。
        matched = teach_log.question_for_parent(self.home, parent_id)
        if matched is None or matched.get('chat_id') != chat_id:
            return False
        pending = [record for record in teach_log.pending_questions(self.home)
                   if record.get('chat_id') == chat_id]
        if not pending:
            return False
        try:
            create_ms = int(message.get('create_time'))
        except (TypeError, ValueError):
            create_ms = int(now * 1000)
        answer_text = message_text(message)
        if not answer_text.strip():
            self._teach_reply(message, '这条我没读出文字内容，没法当答复；把要教的写成一句话再发一次。')
            return True
        logged = teach_log.answer_question(self.home, matched['qid'], answer_text, mid,
                                           at_ms=create_ms)
        if logged.get('duplicate'):
            logging.info('teach answer duplicate qid=%s mid=%s', matched['qid'], mid)
            return True
        replayed, reason = self._replay_question(matched, answer_text, now)
        lines = ['收到你的答复：%s' % answer_text.strip()[:80]]
        if replayed:
            lines.append('· 已经去回原来那条消息了（重放了带本人答复的事件）。')
        else:
            lines.append('· 原来那条消息这次没能重新排上：%s。答过的内容我记着了，'
                         '下次有人再问就按你说的办。' % reason)
        lines.append(teach_log.PERMISSION_LINE)
        self._teach_reply(message, '\n'.join(lines))
        logging.info('teach answer qid=%s mid=%s replayed=%s', matched['qid'], mid, replayed)
        return True

    def _replay_question(self, question, answer_text, now):
        """把问答结论送回原来那条消息：重放一个带 owner_answer 的 message 事件。"""
        event = self.store.get_event(int(question['event_id']))
        payload = None
        if event is not None:
            try:
                payload = json.loads(event['payload'])
            except (TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            return False, '原始事件已经不在库里了'
        target = payload.get('message_id')
        try:
            cap = int(self.config.get('app_daily_reply_cap', 20))
        except (TypeError, ValueError):
            cap = 20
        row = self.store.message_row(target) if isinstance(target, str) else None
        if row is not None and self.store.app_reply_blocked(row, now, daily_cap=cap):
            self.store.set_meta('teach_followup_blocked_total',
                                (self.store.meta('teach_followup_blocked_total') or 0) + 1)
            return False, 'app 每日回复配额用完了（%d 条/天）' % cap
        followup = dict(payload, owner_answer=answer_text)
        dedupe = 'followup:%s:%s' % (target, question['qid'])
        try:
            # 返回 False 表示同 dedupe_key 的事件已经在库里——那正是幂等，不算失败。
            self.store.emit_event('message', dedupe, followup, now)
        except ValueError as error:
            return False, '重放被状态库拒绝：%s' % error
        return True, None


    # ---- 任务卡与审批（S2）--------------------------------------------------
    #: 待批卡的判定：``open`` 且没有 ``approval_mid``。已经批过的 open 不会停在 open，
    #: 这条过滤是为了挡"批完又被 reopen"的中间态——那种情况下卡不该再被批一次。
    def _asked_again_recently(self, task_id, now):
        """这张卡十分钟内是否已经追问过（含糊答复只追问一次，见 approval.ASK_AGAIN_THROTTLE_SECONDS）。"""
        from . import approval

        try:
            last = float(self.store.meta('ask_again:%s' % task_id, 0) or 0)
        except (TypeError, ValueError):
            return False
        return last > 0 and (now - last) < approval.ASK_AGAIN_THROTTLE_SECONDS

    def _mark_asked_again(self, task_id, now):
        self.store.set_meta('ask_again:%s' % task_id, now)

    def pending_cards(self):
        return [task for task in self.store.tasks('open', limit=50)
                if not task.get('approval_mid')]

    def try_task_approval(self, message, now, chat_id):
        """主人对任务卡的答复：三态 + 两档判据 → 改账本 → 回群说一句。

        返回 True = 这条消息已经按审批处理完，不再进事件管道（也不会被当教材学走）。

        为什么这里只调 ``approval.judge``：三态、拒绝词、两档判据（``quoted`` /
        ``unique_pending``）都只在 ``bridge/approval.py`` 定义一次；服务只负责"找到他批的是
        哪张卡"和"把结果说出来"。同一个判断写两遍，下一站一定是两份语义各自演化。
        """
        from . import approval

        if message.get('msg_type') not in ('text', 'post'):
            return False
        mid = message.get('message_id')
        if not isinstance(mid, str) or not mid:
            return False
        text = message_text(message)
        quoted = message.get('parent_id')
        cards = self.pending_cards()
        task = None
        if isinstance(quoted, str) and quoted:
            candidate = self.store.task_by_message(quoted)
            # 引用的必须是"这张卡自己"（或这条任务上的消息），且这张卡还在等批。
            if (candidate is not None and candidate.get('state') == 'open'
                    and not candidate.get('approval_mid')):
                task = candidate
        elif cards and approval.has_decision_word(text):
            # 没有引用：先要求"话里有决定词"，否则主人随口说「帮我查下天气」会被判成批准。
            # 只有一张时这就是批准；有好多张时判据会返回 ambiguous——那就回一句"引用哪一张"，
            # 而不是把消息静默交给模型（那等于他批了、谁也没动）。
            task = cards[0]
        if task is None:
            return False
        sender = message.get('sender') or {}
        decision = approval.judge(
            text=text, sender_id=sender.get('id'),
            owner_id=self.config.get('target_open_id'),
            owner_ids=self.transport.owner_open_ids(),
            chat_type='p2p',
            quoted_mid=quoted if isinstance(quoted, str) else None,
            card_mid=quoted if isinstance(quoted, str) else None,
            pending_count=len(cards),
            sender_is_app=(sender.get('sender_type') == 'app'))
        verdict = decision['verdict']
        if verdict == 'ignore':
            return False
        if verdict == 'ask_again':
            # 两种"没判出态度"的说法给不同追问：空答复是"没写内容"，含糊答复是"没表态"。
            # 引用只证明在答这张卡，不证明同意做（2026-11-17 收紧，见 docs/HISTORY.md）。
            if decision.get('reason') == 'no_decision_word':
                if self._asked_again_recently(task['id'], now):
                    # 十分钟内已经追过一次：交回正常分流，别为了同一张卡连追两条。
                    return False
                self._mark_asked_again(task['id'], now)
                self._teach_reply(
                    message,
                    '这句我没看出你要不要做，不敢动手。要我做就回「做」（要改就写「做，但…」），'
                    '不做就回「不做」。')
            else:
                self._teach_reply(message, '这条我没读出内容，没法当答复。要批就说「做」，不批就说「不做」。')
            return True
        if verdict == 'ambiguous':
            self._teach_reply(
                message,
                '现在有 %d 张卡在等你点头，光说一句我分不清是哪一张：请**引用那张卡**再回我。'
                % len(cards))
            return True
        if verdict == 'approve':
            result = self.store.approve_task(
                task['id'], approval_mid=mid, approval_kind=decision['kind'],
                hints_text=decision['hints'], now=now)
            if result.get('ok'):
                self._announce_card(task, 'approve', decision['hints'],
                                    duplicate=result.get('duplicate'))
                logging.info('task approved id=%s kind=%s mid=%s', task['id'],
                             decision['kind'], mid)
            else:
                self._teach_reply(message, '这张卡现在不是"等批准"状态了（可能已经批过或作废），我没改账本。')
            return True
        result = self.store.reject_task(task['id'], approval_mid=mid, now=now)
        if result.get('ok'):
            self._announce_card(task, 'reject', decision['hints'],
                                duplicate=result.get('duplicate'))
            logging.info('task rejected id=%s mid=%s', task['id'], mid)
        else:
            self._teach_reply(message, '这张卡现在不是"等批准"状态了，我没改账本。')
        return True

    def _announce_card(self, task, verdict, hints, duplicate=False):
        """把"批了/不批"说回群里（挂在提出这条活的原始消息下）。说错话比不说更糟，所以：
        先 announce 进 outbox（sender 统一发），失败只记日志，回滚账本不是选项——
        账本记录的是"主人批了"，那句话发不出去是通道问题，不是批准无效。"""
        said = ' '.join((hints or '').split())[:120]
        if verdict == 'approve':
            body = '主人批了这张卡：「%s」。我按卡上的步骤去办，办完回这里。' % said
        else:
            body = '主人说这张不办：「%s」。不做了。' % said
        self._say_to_task(task, body, verdict)

    def _say_to_task(self, task, body, scope):
        try:
            result = self.store.announce(
                time.time(), body,
                inbox_id=task.get('origin_mid') or None,
                chat_id=None if task.get('origin_mid') else task.get('chat_id'),
                uuid='bridge-task-%s-%s' % (task['id'], scope))
            if not result.get('ok'):
                logging.warning('task announce rejected id=%s reason=%s', task['id'],
                                result.get('reason'))
        except Exception:  # noqa: BLE001 - 说话失败不能让账本回滚
            logging.exception('task announce failed id=%s', task['id'])

    def expire_task_cards(self, now):
        """过点没批的卡作废，并在群里说一句（每拍调一次，幂等：作废后不再是 open）。"""
        from . import approval

        def announce(task):
            self._say_to_task(task, '这张卡过了 %d 分钟没人点头，我作废了。要办的话重新 @ 我说一遍。'
                              % int(approval.CARD_TTL_SECONDS / 60), 'expired')

        try:
            expired = approval.expire_cards(self.store, now, announce=announce)
        except Exception:  # noqa: BLE001 - 清理失败不能让服务停
            logging.exception('task card expiry failed')
            return 0
        if expired:
            logging.info('expired %d task card(s)', len(expired))
        return len(expired)

    def _teach_reply(self, message, body):
        try:
            self.transport.reply(message, body, reply_uuid(message.get('message_id'), 'teach'))
        except Exception as error:  # noqa: BLE001 - 回执失败不该让教材本身回滚
            self.error('teach', error)

    def replyable_chats(self):
        """允许回复的会话：已确认在册的群 + 教材私聊。

        教材私聊是 p2p，永远不在 inventory 的群清单里；只按"群清单"判会把私聊的每一条回复
        都当成「群已不在清单」——真机 16:08 就这么丢掉了一条已经生成好的回答（needs_attention）。
        """
        chats = {group['chat_id'] for group in self.store.groups() if group.get('present')}
        teach_chat = teach_log.enabled_chat(self.home)
        if teach_chat:
            chats.add(teach_chat)
        return chats

    def control_chat_ids(self):
        """控制指令（下班/上班）允许出现的会话。

        语义交给身份层：``as_user`` 轮询自聊群 + 教材私聊；``as_bot`` 有长连接推 DM，
        不需要轮询（返回空集 = "不需要"，不是"不支持"）。业务代码不判断模式。
        """
        return set(self.transport.control_chat_ids(
            self.config, teach_chat=teach_log.enabled_chat(self.home)))

    def is_control_message(self, message):
        sender = (message.get('sender') or {}).get('id')
        chat_id = message.get('chat_id')
        if not self.transport.is_owner(sender):
            return False
        if chat_id not in self.control_chat_ids():
            return False
        return command_table.match(message_text(message)) is not None

    def handle_control_message(self, message, now):
        mid = message.get('message_id')
        if not isinstance(mid, str) or not mid.startswith('om_'):
            return False
        with self._control_lock:
            if mid in self._control_seen:
                return True
        self._remember_control(mid)
        if not self.is_control_message(message):
            return False
        try:
            create_ms = int(message.get('create_time'))
        except (TypeError, ValueError):
            return False
        floor = max(self.store.meta('hold_entry_applied_ms') or 0,
                    self.store.meta('hold_release_applied_ms') or 0)
        if create_ms < floor:
            return False
        matched = command_table.match(message_text(message))
        if matched is None:
            return False
        verb = matched[0]
        try:
            notice = self.enter_hold('feishu', now) if verb == 'hold' else self.release_hold('feishu', now)
            if notice:
                self.transport.reply(message, notice, reply_uuid(mid, 'control'))
        except Exception as error:
            self.error('control', error)
            return False
        logging.info('control command=%s mid=%s', verb, mid)
        return True

    def _remember_control(self, mid):
        with self._control_lock:
            self._control_seen[mid] = True
            self._control_order.append(mid)
            while len(self._control_order) > 500:
                self._control_seen.pop(self._control_order.pop(0), None)

    def external_config(self):
        """home/external.json 只提供外部消费者的回复超时（atomic, mtime-cached）。

        `enabled` 语义已删除：内置 analyst 删掉之后，外部消费者就是唯一的大脑，
        不存在"关掉 external 退回内置"这条路（见 docs/superpowers/specs 与 docs/HISTORY.md）。
        """
        path = self.home / 'external.json'
        try:
            stat = path.stat()
        except OSError:
            return {}
        key = (stat.st_mtime_ns, stat.st_size)
        if self._external_key != key:
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                value = {}
            self._external = value if isinstance(value, dict) else {}
            self._external_key = key
        return self._external

    def external_reply_timeout(self):
        try:
            value = float(self.external_config().get('reply_timeout_seconds', 600))
        except (TypeError, ValueError):
            return 600.0
        return value if value > 0 else 600.0

    TASK_STEP_HISTORY = 5
    TASK_ARTIFACT_HISTORY = 10

    def task_summary(self, task):
        """给消费者看的最小账本：够它判断"这活做到哪了"，又不会把 payload 撑大。"""
        steps = task.get('steps') or []
        return {'id': task['id'], 'title': task['title'], 'state': task['state'],
                'steps': [{'n': step.get('n'), 'outcome': step.get('outcome'),
                           'note': (step.get('note') or '')[:200]}
                          for step in steps[-self.TASK_STEP_HISTORY:]],
                'artifacts': [item.get('path') for item in (task.get('artifacts') or [])
                              if isinstance(item, dict) and item.get('path')][:self.TASK_ARTIFACT_HISTORY],
                'open_question': task.get('open_question'),
                'updated_at': task.get('updated_at')}

    def task_context(self, message, mid, text, self_intent, now):
        """这条消息属于哪个任务：引用/回复了任务里的消息就挂上去，否则没有账。

        建任务的唯一入口是**任务卡**（``task draft``，主人批准后才 running）。这里不再
        "判出派活就自动建账"——那条旧路让账本里出现一堆没人批过的活，还会绕开审批线
        （2026-11-17 收掉，见 docs/plan-s2-tasks-approval.md PR-3）。

        返回 (task_meta, error)。task_meta 是写进事件载荷的那一段；error 非空表示建账失败
        （状态库写不进去时，消息照旧交出去，只在日志里留痕——账本不该拦消息）。
        """
        try:
            for candidate in (message.get('parent_id'), message.get('root_id')):
                if not isinstance(candidate, str) or not candidate:
                    continue
                existing = self.store.task_by_message(candidate)
                if existing is None:
                    continue
                if not self.store.attach_task_message(existing['id'], mid, now,
                                                      chat_id=message.get('chat_id')):
                    continue
                if existing['state'] not in Store.TASK_ACTIVE_STATES:
                    # 引用一个已经结案的任务说话 = 本人要接着做（真机踩过：消费者在第 2 步
                    # 就报了 done，第 3 步的回复于是被"结案任务不吃新步骤"的守卫丢掉）。
                    # 重开账本继续记，而不是另开一个新任务——同一个活的步骤应该在一本账上。
                    reopened = self.store.set_task_state(existing['id'], 'open', now)
                    existing = reopened or existing
                step = len(existing.get('steps') or []) + 1
                return {'id': existing['id'], 'title': existing['title'], 'step': step}, None
        except Exception as error:  # noqa: BLE001 - 账本是旁路，不能因为它丢消息
            return None, '%s: %s' % (type(error).__name__, str(error)[:200])
        return None, None

    def external_payload(self, message, mid, text=None, self_intent=None, task=None,
                         chat_type=None):
        """Everything an external session needs to answer, including the anchored context.

        ``chat_type`` 是"这条消息在群里还是私聊"（模型据此决定说话的口气，以及能不能不回）；
        ``mentions`` 把正文里的 ``@_user_1`` 占位符翻成人名——不给这张表，模型只能猜，
        真机踩过：它把占位符原样抄进了回复。
        """
        sender = message.get('sender', {})
        context, context_error = [], None
        try:
            context = self.transport.context(message)
        except Exception as error:
            context_error = '%s: %s' % (type(error).__name__, str(error)[:300])
            logging.warning('external context failed mid=%s %s', mid, context_error)
        payload = {'message_id': mid, 'chat_id': message.get('chat_id'),
                   # 本人发这条消息的本地时刻：模型据此把"1 小时后"算成绝对时间（提醒）。
                   # 给现成的人话时间而不是毫秒，是因为模型做毫秒加法容易错到别的日期。
                   'create_at': time.strftime('%Y-%m-%d %H:%M:%S',
                                              time.localtime(int(message['create_time']) / 1000)),
                   'sender_id': sender.get('id'), 'sender_type': sender.get('sender_type'),
                   'text': (text if isinstance(text, str) and text else message_text(message))[:16000],
                   'create_ms': int(message['create_time']),
                'thread_id': message.get('thread_id'), 'parent_id': message.get('parent_id'),
                   'context': context, 'context_error': context_error,
                   'reply': {'via': 'feishu-bridge events reply', 'uuid': reply_uuid(mid)}}
        if chat_type in ('group', 'p2p'):
            payload['chat_type'] = chat_type
        mentions = [{'key': item.get('key'), 'name': item.get('name')}
                    for item in (message.get('mentions') or [])
                    if isinstance(item, dict) and item.get('key')]
        if mentions:
            payload['mentions'] = mentions[:20]
        if self_intent:
            payload['self_intent'] = self_intent
        if task:
            payload['task'] = task
        try:
            active = [self.task_summary(item)
                      for item in self.store.active_tasks(message.get('chat_id'))]
        except Exception:  # noqa: BLE001 - 账本读不出来时退化成"没有任务"，消息照旧
            active = []
        if active:
            payload['tasks'] = active
        return payload

    def config_seconds(self, name, default):
        try:
            return float(self.config.get(name, default))
        except (TypeError, ValueError):
            return float(default)

    def collector_watermark_lag(self, scan_name, epoch, now):
        scan = self.store.get_scan(scan_name) or {}
        watermark = scan.get('watermark_ms')
        if watermark is None:
            live = self.store.get_scan('live') or {}
            watermark = live.get('watermark_ms')
        if watermark is None:
            watermark = epoch
        return round((now * 1000 - watermark) / 1000, 1)

    def collector_health(self, scan_name, width, failures, last_success_at):
        try:
            epoch = self.store.meta('service_epoch_ms') or int(time.time() * 1000)
            snapshot = {'window_ms': width, 'watermark_lag_s': self.collector_watermark_lag(scan_name, epoch, time.time()),
                        'consecutive_failures': failures, 'last_success_at': last_success_at}
        except Exception:
            logging.exception('collector health snapshot failed')
            return
        with self.health_lock:
            self.health['collector'] = snapshot

    def collector(self):
        next_identity = next_inventory = next_live = 0
        next_backfill = 0  # P2-14: run the 24h safety-net sweep as soon as the service starts.
        next_self = 0  # 教材私聊：启用教学之后才真正参与调度。
        failures = {'live': 0, 'backfill': 0, 'self': 0}
        last_success_at = None
        while not self.stop.is_set():
            now = time.time()
            scan_name = None
            try:
                if now >= next_identity:
                    next_identity = now + 60
                    self.identity = self.transport.identity()
                    self.authenticated.set()
                    if self.store.meta('service_epoch_ms') is None:
                        self.store.set_meta('service_epoch_ms', int(time.time() * 1000))
                    self.success('identity')
                if not self.authenticated.is_set():
                    self.stop.wait(2)
                    continue
                self.sync_hold(now)
                # 控制通道常驻：挂起时它是唯一活动；正常态也要跑，否则自聊里的纯文本
                # "下班"永远进不了 search（search 固定要求 @我 + 群聊）。
                self.control_tick(now)
                if self.held():
                    if not self._was_held:
                        self._was_held = True
                        logging.info('bridge held: collector idle, no search, no queue writes')
                    # 冻结所有扫描：不搜索就不可能入队，这是"零入队"的唯一可靠保证。
                    next_live = next_backfill = next_self = 0
                    self.stop.wait(1)
                    continue
                if self._was_held:
                    self._was_held = False
                    next_live = next_backfill = next_self = 0
                    logging.info('bridge resumed: collector active, off-window stays excluded')
                if now >= next_inventory:
                    next_inventory = now + 60
                    self.store.register_groups(self.transport.inventory(), time.time())
                    self.success('inventory', 'last_inventory_at')
                # Pick the most overdue scan so a struggling scan cannot starve the others.
                self.ensure_self_channel()
                self.sync_self_scan()
                due = []
                if now >= next_live:
                    due.append((next_live, 'live'))
                if now >= next_backfill:
                    due.append((next_backfill, 'backfill'))
                if teach_log.enabled_chat(self.home) and now >= next_self:
                    due.append((next_self, 'self'))
                if due:
                    scan_name = min(due)[1]
                if scan_name is None:
                    self.stop.wait(self.config_seconds('collector_poll_seconds', 0.5))
                    continue
                # Failure counts are per scan: a poisoned live cursor must not be reset merely
                # because backfill happened to fail five times in a row (and vice versa).
                try:
                    outcome = self.collect_scan(scan_name)
                except Exception as error:
                    failures[scan_name] += 1
                    count = failures[scan_name]
                    retryable = getattr(error, 'retryable', True)
                    kind = getattr(error, 'kind', None)
                    reset = ((isinstance(error, TransportError) and not retryable)
                             or count >= MAX_SCAN_FAILURES
                             or ('token' in str(error).lower() and kind != 'authorization'))
                    if reset:
                        self.store.reset_scan(scan_name)
                        failures[scan_name] = 0
                        logging.warning('search %s reset after failure: %s', scan_name, str(error)[:300])
                    self.error('collector', error)
                    width = max(WINDOW_FLOOR_MS, int(self.store.meta(scan_name + '_window_ms', WINDOW_DEFAULT_MS)))
                    if scan_name == 'live':
                        next_live = self.scan_retry_time(now, failures[scan_name])
                    elif scan_name == 'self':
                        next_self = self.scan_retry_time(now, failures[scan_name])
                    else:
                        next_backfill = self.scan_retry_time(now, failures[scan_name])
                else:
                    width = outcome['width']
                    if outcome.get('skipped'):
                        # 区间还没宽到能问（见 MIN_SCAN_SPAN_MS）：不是失败，别计失败次数，
                        # 隔一拍再来。
                        failures[scan_name] = 0
                        if scan_name == 'live':
                            next_live = now + 1
                        elif scan_name == 'self':
                            next_self = now + 1
                        else:
                            next_backfill = now + 1
                    elif outcome['notice'] is not None:
                        # A notice means the page was truncated: ingest what we got, shrink the
                        # configured window, drop the cursor and retry with backoff.
                        failures[scan_name] += 1
                        logging.warning('search %s incomplete notice=%s width=%sms', scan_name,
                                        str(outcome['notice'])[:250], width)
                        if scan_name == 'live':
                            next_live = self.scan_retry_time(now, failures[scan_name])
                        elif scan_name == 'self':
                            next_self = self.scan_retry_time(now, failures[scan_name])
                        else:
                            next_backfill = self.scan_retry_time(now, failures[scan_name])
                    else:
                        failures[scan_name] = 0
                        last_success_at = time.time()
                        if scan_name == 'live':
                            next_live = time.time() + self.config_seconds('live_interval_seconds', 3)
                        elif scan_name == 'self':
                            next_self = time.time() + self.config_seconds('self_scan_seconds', 10)
                        elif outcome['token'] is None:
                            self.store.set_meta('backfill_position_ms', outcome['end_ms'])
                            target = self.store.meta('backfill_target_ms', outcome['end_ms'])
                            if outcome['end_ms'] >= target:
                                self.store.set_meta('backfill_target_ms', None)
                                next_backfill = time.time() + self.config_seconds('backfill_sweep_seconds', 3600)
                            else:
                                next_backfill = time.time() + self.config_seconds('backfill_interval_seconds', 60)
                        else:
                            next_backfill = time.time() + self.config_seconds('backfill_interval_seconds', 60)
                self.collector_health(scan_name, width, failures[scan_name], last_success_at)
                self.stop.wait(self.config_seconds('collector_poll_seconds', 0.5))
            except Exception as error:
                self.error('collector', error)
                self.stop.wait(10)

    def scan_retry_time(self, now, failures):
        backoff = self.config_seconds('collector_max_backoff_seconds', 300)
        base = self.config_seconds('collector_backoff_seconds', 10)
        return now + min(backoff, base * max(1, 2 ** (failures - 1)))

    def p2p_readable(self):
        """私聊正文读得到吗：缺 ``im:message.p2p_msg:get_as_user`` 就只能扫到 id、取不到正文。

        检索（``search:message``）和读正文是两套权限，所以"能搜到"不代表"能答"；
        这条通道因此必须在权限缺失时明确不跑，而不是每来一条消息报一次 access denied。
        结果按 5 分钟缓存：授权可以随时补，但不值得每一拍去问一次 ``auth status``。
        """
        now = time.time()
        if self._p2p_ok is not None and now - self._p2p_checked_at < 300:
            return self._p2p_ok
        try:
            self._p2p_ok = teach_log.P2P_READ_SCOPE in self.transport.user_scopes()
        except Exception as error:  # noqa: BLE001 - 读不出授权就当读不到，别让通道误开
            logging.warning('self chat: 读不出授权范围（%s）', type(error).__name__)
            self._p2p_ok = False
        self._p2p_checked_at = now
        return self._p2p_ok

    def ensure_self_channel(self):
        """默认开通私聊通道：没写过 teach.json 就自己认出本人会话并登记上去。

        以前这条路要求先 ``teach enable --chat <oc_…>``，可那个 chat_id 在飞书界面上
        根本看不到——用户没有任何能自己做对的动作，于是"默认关"对每个新机器都是死通道
        （同事机 2026-11-06：私聊里 @ 自己问黑五价格，一条都没进来，也没有任何地方报错）。
        所以：没有显式配置 = 默认开，会话由 ``p2p_target_id == 本人`` 算出来并落进 teach.json；
        显式 ``teach disable`` 一律尊重，服务不会把它按默认值重新打开。
        """
        if teach_log.read_config(self.home):
            return  # 显式配置（含 enabled:false、含坏配置）都不由默认值覆盖
        now = time.time()
        if self.store.meta('self_channel_first_try_ms') is None:
            # 记下"第一次尝试认会话"的时刻：区分"刚启动、马上就好"和"这个账号根本没有
            # 自己跟自己的会话"（后者要告诉人去飞书里发一条，干等下去永远好不了）。
            self.store.set_meta('self_channel_first_try_ms', int(now * 1000))
        if now - self._self_channel_try_at < 60:
            return
        self._self_channel_try_at = now
        if not self.p2p_readable():
            logging.warning('self chat channel stays off: token 缺 im:message.p2p_msg:get_as_user，'
                            '补齐后会自动开通（teach status 也会一直显示未开通）')
            return
        try:
            chat_id = self.transport.self_chat_id()
        except Exception as error:  # noqa: BLE001 - 认不出就下一分钟再试，别写半个登记
            logging.warning('self chat channel: 认不出本人会话（%s）', str(error)[:200])
            return
        teach_log.write_config(self.home, teach_log.default_channel(chat_id, int(now * 1000)))
        logging.info('self chat channel enabled by default: %s', chat_id)

    def sync_self_scan(self):
        """教材私聊扫描只在教学启用期间存在：停用就清掉游标。

        注册交给 `collect_scan` 的首次分支（那里有 `scan_bounds` 与 origin 夹取，是唯一一份
        窗口算法）。这里如果再算一遍起点，就会出现"两个地方算同一个窗口"——第一次真机跑
        就是这么炸的：注册时 start==end，飞书直接拒（start_time must be earlier than end_time）。
        """
        if teach_log.enabled_chat(self.home):
            return
        scan = self.store.get_scan('self')
        if scan is not None and scan.get('active'):
            self.store.reset_scan('self')
            logging.info('self scan removed (teaching disabled)')

    def collect_scan(self, scan_name):
        """Run exactly one search page. Returns ids/cursor/notice; raises on transport failure."""
        epoch = self.store.meta('service_epoch_ms') or 0
        width = max(WINDOW_FLOOR_MS, int(self.store.meta(scan_name + '_window_ms', WINDOW_DEFAULT_MS)))
        scan = self.store.get_scan(scan_name)
        if not scan or not scan.get('active') or not _usable_bounds(scan):
            # 坏游标（start >= end，例如旧版本注册时写坏的）必须当场重算：否则每一拍都会拿同一对
            # 非法边界去问飞书，日志里只剩 WARNING，扫描看着"在跑"其实一条也搜不到。
            target = int(time.time() * 1000)
            if scan_name == 'self':
                # 教材私聊：起点不早于「启用教学」那一刻，绝不回填启用之前的私聊历史。
                origin_ms = max(epoch, int(teach_log.read_config(self.home).get('enabled_at_ms') or 0))
                start, end = scan_bounds(epoch, target, target, width, origin_ms)
            elif scan_name == 'backfill':
                saved_target = self.store.meta('backfill_target_ms')
                if saved_target is None:
                    saved_target = target
                    self.store.set_meta('backfill_target_ms', saved_target)
                    self.store.set_meta('backfill_position_ms', max(epoch, saved_target - WINDOW_DEFAULT_MS))
                origin = max(epoch, saved_target - WINDOW_DEFAULT_MS)
                position = self.store.meta('backfill_position_ms', origin)
                start, end = scan_bounds(epoch, position, saved_target, width, origin)
            else:
                position = (scan or {}).get('watermark_ms', epoch)
                start, end = scan_bounds(epoch, position, target, width)
            if end - start < MIN_SCAN_SPAN_MS:
                # 刚启用教学的那几拍：可用区间只有几百毫秒（下界是"启用那一刻"），截到秒就是
                # 同一个时刻。这一拍不注册、也不搜——等时间自己走出一秒以上，绝不为了凑宽度
                # 回填启用之前的私聊历史。
                return {'notice': None, 'width': width, 'token': None, 'end_ms': end,
                        'skipped': True}
            self.store.begin_scan(scan_name, start, end)
            scan = self.store.get_scan(scan_name)
        chat_id = teach_log.enabled_chat(self.home) if scan_name == 'self' else None
        # 推送型身份（as_bot 的长连接）没有"@我"的搜索接口，但它能按窗口对账：
        # 这里只问"这个身份有没有对账能力"，**不判断模式**（能力在实现类里）。
        collector_capability = getattr(self.transport, 'collect', None)
        reconciled = (collector_capability(scan_name, scan['start_ms'], scan['end_ms'])
                      if (chat_id is None and collector_capability is not None) else None)
        if reconciled is not None:
            result = reconciled
        else:
            result = self.transport.search(scan['start_ms'], scan['end_ms'], scan.get('page_token'),
                                           chat_id=chat_id)
        ids, token, notice = list(result['ids']), result['next_token'], result.get('notice')
        if notice is not None:
            if ids:
                self.store.ingest_partial_ids(scan_name, ids, time.time())
            width = max(WINDOW_FLOOR_MS, width // 2)
            self.store.set_meta(scan_name + '_window_ms', width)
            self.store.set_meta(scan_name + '_window_successes', 0)
            self.store.reset_scan(scan_name)
            return {'notice': notice, 'width': width, 'token': token, 'end_ms': scan['end_ms']}
        self.store.ingest_search_page(scan_name, ids, token, time.time())
        if token is None:
            successes = self.store.meta(scan_name + '_window_successes', 0) + 1
            if successes >= WINDOW_GROWTH_SUCCESSES:
                width = min(WINDOW_DEFAULT_MS, width * 2)
                self.store.set_meta(scan_name + '_window_ms', width)
                self.store.set_meta(scan_name + '_window_successes', 0)
                logging.info('search %s window recovered to %sms', scan_name, width)
            else:
                self.store.set_meta(scan_name + '_window_successes', successes)
        self.success('collector', 'last_search_at')
        logging.info('search %s hits=%s more=%s', scan_name, len(ids), bool(token))
        return {'notice': None, 'width': width, 'token': token, 'end_ms': scan['end_ms']}

    def details(self):
        while not self.stop.is_set():
            if not self.authenticated.is_set() or self.held():
                self.stop.wait(2)
                continue
            job = None
            try:
                job = self.store.claim_message('details', time.time(), 150)
                if not job:
                    group = self.store.next_group_probe(time.time())
                    if group:
                        try:
                            self.transport.probe_group(group['chat_id'])
                            self.store.group_access(group['chat_id'], True, None, time.time())
                        except TransportError as error:
                            self.store.group_access(group['chat_id'], False, str(error), time.time())
                            self.error('group_probe', error)
                    self.stop.wait(1)
                    continue
                mid = job.get('message_id') or job.get('id')
                message = self.transport.get_message(mid)
                sender = message.get('sender', {})
                chat_id = message.get('chat_id')
                teach_chat = teach_log.enabled_chat(self.home)
                self_chat = chat_id in self_chat_ids(self.config) or (teach_chat and chat_id == teach_chat)
                groups = {g['chat_id'] for g in self.store.groups() if g.get('present')}
                if chat_id not in groups and not self_chat:
                    self.store.register_groups(self.transport.inventory(), time.time())
                    groups = {g['chat_id'] for g in self.store.groups() if g.get('present')}
                reason = None
                if message.get('deleted'):
                    reason = 'recalled'
                elif not self.transport.is_mentioned(message):
                    reason = 'not_personal_mention'
                elif (self.transport.needs_self_guard
                      and sender.get('id') == self.config['target_open_id']):
                    # 个人账号要防"回自己的话"（自聊 opt-in）；机器人天然不需要这条守卫。
                    if not self_chat:
                        reason = 'own_message'
                elif sender.get('sender_type') == 'app' and self.config.get('reply_to_bots') is False:
                    # 别的机器人 @ 本人：不是人发的，回了就是 AI 互刷（真机上一条机器人长回复就能
                    # 把每日回复配额刷光，还会堆出一串 needs_attention / 死事件）。
                    # 默认保持旧行为（回复）；用 `feishu-bridge bots --disable` 显式关掉。
                    reason = 'bot_message'
                elif sender.get('sender_type') not in ('user', 'app'):
                    reason = 'unsupported_sender'
                elif int(message['create_time']) < self.store.meta('service_epoch_ms'):
                    reason = 'before_service_activation'
                elif chat_id not in groups and not self_chat:
                    reason = 'not_joined_group'
                fields = {'chat_id': message.get('chat_id'), 'parent_id': message.get('parent_id'),
                          'root_id': message.get('root_id'), 'thread_id': message.get('thread_id'),
                          'create_ms': int(message['create_time']), 'version': message_version(message),
                          'raw_json': json.dumps(message, ensure_ascii=False), 'text': message_text(message)[:16000],
                          'sender_type': sender.get('sender_type'), 'sender_id': sender.get('id')}
                # 私聊里的精确命令：**只认 bridge/commands.py 那张表**（确定性字符串匹配），
                # 不走模型推断；群里一律不执行（命令是主人对桥说话，不是群成员对桥说话）。
                # 命令通道：as_user=白名单自聊群；as_bot=主人与机器人的私聊（身份层判定）
                private_chat = self.transport.command_chat_ok(
                    chat_id, self_chat_ids=self_chat_ids(self.config),
                    chat_type=message.get('chat_type'), known_group=chat_id in groups)
                is_owner = (self.transport.is_owner(sender.get('id'))
                            and sender.get('sender_type') != 'app'
                            and not message.get('deleted'))
                command = command_table.match(message_text(message)) if (private_chat and is_owner) else None
                if command is not None:
                    verb, payload = command
                    self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                                state='ignored', error='command:%s' % verb)
                    self.run_command(verb, payload, chat_id, mid, time.time(),
                                     text=message_text(message))
                    self.store.group_access(chat_id, True, None, time.time())
                    self.success('details')
                    self.stop.wait(0.2)
                    continue
                if (private_chat and is_owner
                        and command_table.looks_like_command(message_text(message))
                        and self._guide_ready(chat_id, time.time())):
                    # 近似说法（停一下/继续/下班了）一律不执行，但也不许静默：回一句命令要精确。
                    # 有节流，免得同一句话反复发变成刷屏。
                    self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                                state='ignored', error='command_unclear')
                    self.store.announce(time.time(), command_table.guide(), inbox_id=mid,
                                        uuid='guide:%s' % mid)
                    self.store.group_access(chat_id, True, None, time.time())
                    self.success('details')
                    self.stop.wait(0.2)
                    continue
                if self.is_control_message(message):
                    # 控制指令不是业务消息：先落成终态 ignored（否则挂起动作里的清队会把它
                    # 改成 dropped），再执行开关，绝不交给大脑。
                    self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                                state='ignored', error='control_command')
                    self.handle_control_message(message, time.time())
                    self.stop.wait(0.2)
                    continue
                quoted_task = None
                if reason is None:
                    try:
                        for candidate in (message.get('parent_id'), message.get('root_id')):
                            quoted_task = (self.store.task_by_message(candidate)
                                           if isinstance(candidate, str) and candidate else None)
                            if quoted_task is not None:
                                break
                    except Exception as error:  # noqa: BLE001 - 账本查询失败按"没在谈任务"处理
                        logging.warning('task lookup failed mid=%s %s', mid, error)
                if reason is None and teach_chat and chat_id == teach_chat and quoted_task is not None:
                    # 引用任务里的消息：先问一句"这是不是批准"，不是才原样交给事件管道继续这个活。
                    # 这条分支必须在这里，不能只放在 route_self_chat 里——那个函数只处理
                    # "没引用任何东西"的私聊消息（详情管道在引用任务时根本不走它，见下）。
                    if self.try_task_approval(message, time.time(), chat_id):
                        self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                                    state='ignored', error='task_approval')
                        self.store.group_access(chat_id, True, None, time.time())
                        self.success('details')
                        self.stop.wait(0.2)
                        continue
                    self_intent = None
                elif reason is None and teach_chat and chat_id == teach_chat:
                    # 教材私聊的唯一处理点：教材/指令就地办掉，提问/派活按原样交给事件管道。
                    override = self.route_self_chat(message, time.time())
                    if override is None:
                        self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                                    state='ignored', error='teach_channel')
                        self.store.group_access(chat_id, True, None, time.time())
                        self.success('details')
                        self.stop.wait(0.2)
                        continue
                    fields['text'] = (override.get('text') or fields.get('text', ''))[:16000]
                    self_intent = override.get('self_intent')
                elif reason is None and teach_chat and chat_id == teach_chat:
                    # 引用任务里的消息：不判意图、不进教材候选，原样交给事件管道继续这个活。
                    self_intent = None
                else:
                    self_intent = None
                # 唯一的大脑是外部消费者：只要这条消息该回，就写成事件交给它。
                # 没有内置 analyst 兜底——消费者不在线时消息停在 external_queued，doctor 报
                # consumer_offline，而不是让另一条能力不同的路替它答（HISTORY §14）。
                state = 'ignored' if reason else 'external_queued'
                event = None
                if reason is None:
                    task_meta, task_error = self.task_context(message, mid, fields.get('text'),
                                                             self_intent, time.time())
                    if task_error:
                        logging.warning('task ledger failed mid=%s %s', mid, task_error)
                    # 群/私聊只有一处判据：这个会话在不在群清单里（inventory 是唯一事实源）。
                    # 不能用 self_chat——它是"允许自己 @ 自己"的白名单，群里 @ 自己派活时它为真，
                    # 却仍是一条群消息（v7 的 chat_type 就是这么被写成 p2p 的，群规矩整段失效）。
                    event = {'kind': 'message', 'dedupe_key': 'message:' + mid,
                             'payload': self.external_payload(message, mid,
                                                             text=fields.get('text'),
                                                             self_intent=self_intent,
                                                             task=task_meta,
                                                             chat_type='group' if chat_id in groups else 'p2p')}
                if self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                               state=state, error=reason, event=event):
                    self.store.group_access(message['chat_id'], True, None, time.time())
                self.success('details')
            except Exception as error:
                self.error('details', error)
                if job:
                    # P2-05: bound details retries exactly like analysis (8 attempts), otherwise a
                    # deterministic retryable error polls the rate-limited CLI forever.
                    permanent = ((isinstance(error, TransportError) and not error.retryable
                                  and error.kind != 'authorization')
                                 or job.get('attempts', 0) >= 8)
                    self.store.retry_message(job.get('message_id') or job.get('id'), job['attempt_id'],
                                             time.time(), str(error), 60, permanent)
                self.stop.wait(2)

    def sender(self):
        """唯一出口：按 outbox 行发。reply 要回读校验，say/retract 发出去即成功。"""
        while not self.stop.is_set():
            if not self.authenticated.is_set() or not self.enabled() or self.held():
                self.stop.wait(2)
                continue
            outgoing = None
            try:
                outgoing = self.store.claim_outbox(time.time(), 120)
                if not outgoing:
                    self.stop.wait(1)
                    continue
                if self.held():
                    # 认领瞬间刚切到挂起：放回队列（恢复时的清队会把它作废，不会补发）。
                    self.store.fail_outbox(outgoing['row_id'], outgoing['attempt_id'],
                                           time.time(), 'Bridge held', 30)
                    continue
                handler = {'reply': self.send_reply_row, 'say': self.send_say_row,
                           'retract': self.send_retract_row}.get(outgoing['kind'])
                if handler is None:
                    self.store.fail_outbox(outgoing['row_id'], outgoing['attempt_id'],
                                           time.time(), 'Unsupported outbox kind', 0, True)
                    continue
                handler(outgoing)
                self.success('sender')
                self.stop.wait(1)
            except Exception as error:
                self.error('sender', error)
                if outgoing:
                    permanent = isinstance(error, TransportError) and not error.retryable and error.kind != 'authorization'
                    self.store.fail_outbox(outgoing['row_id'], outgoing['attempt_id'], time.time(), str(error), 60, permanent)
                self.stop.wait(2)

    def send_reply_row(self, outgoing):
        """kind='reply'：终回复。发出去后还要回读校验，校验通过才置 inbox completed。"""
        # v8：outbox 行号、来源消息 id、目标会话是三个标识符，不许再合成一个 mid。
        row_id, attempt = outgoing['row_id'], outgoing['attempt_id']
        inbox_id = outgoing.get('inbox_id')
        if not inbox_id:
            self.store.fail_outbox(row_id, attempt, time.time(),
                                   'Reply row has no source message', 0, True)
            return
        job = self.store.get_message(inbox_id)
        if job is None:
            self.store.fail_outbox(row_id, attempt, time.time(),
                                   'Source message vanished from inbox', 0, True)
            return
        original = json.loads(job['raw_json'])
        if outgoing['mode'] == 'send':
            fresh = self.transport.get_message(inbox_id)
            if fresh.get('deleted') or message_version(fresh) != job['version']:
                # 来源被撤回/改写：这条回复本来就不该发出去。落明确终态，**不产生待办**——
                # 消息都没了，人也没有可做的事（挂在待人工只会让 doctor 永远 warn）。
                self.store.drop_outbox(row_id, attempt, time.time(),
                                       'Source message recalled or edited; reply dropped',
                                       'source recalled; nothing to send')
                return
            if original['chat_id'] not in self.replyable_chats():
                self.store.fail_outbox(row_id, attempt, time.time(), 'Group no longer in confirmed inventory', 0, True)
                return
            reply_id = self.transport.reply(fresh, outgoing['body'], outgoing['uuid'], before_send=self.send_guard(outgoing))
            if not self.store.outbox_sent(row_id, attempt, time.time(), reply_id):
                logging.error('outbox_sent rejected row=%s attempt=%s', row_id, attempt)
            logging.info('reply sent %s -> %s', inbox_id, reply_id)
            return
        reply = self.transport.get_message(outgoing['reply_id'])
        # as_user 校验本人 open_id；as_bot 校验 app_id——身份层给，不在这里分支。
        reason = verify_reply_reason(reply, original, outgoing['body'], self.transport.self_sender_id())
        if reason:
            # 原因分种类写进 error：正文被改 / @ 的对象不对 / 挂错了消息，运维一眼能分开。
            self.store.fail_outbox(row_id, attempt, time.time(), reason, 0, True)
            return
        if not self.store.outbox_verified(row_id, attempt, time.time()):
            logging.error('outbox_verified rejected row=%s attempt=%s', row_id, attempt)
        logging.info('reply verified %s -> %s', inbox_id, outgoing['reply_id'])

    def send_say_row(self, outgoing):
        """kind='say'：补充说明。有来源消息就回到那条下面，没有就发到会话里。

        与 reply 的差别：不参与回读校验（没有「对方到底看没看见」这回事），也不改 inbox 状态——
        ``outbox_marked()`` 只写 outbox。失败由 ``fail_outbox()`` 按 kind 走 retry_wait/needs_attention。
        """
        row_id, attempt = outgoing['row_id'], outgoing['attempt_id']
        inbox_id, chat_id = outgoing.get('inbox_id'), outgoing.get('chat_id')
        if inbox_id:
            job = self.store.get_message(inbox_id)
            if job is None:
                self.store.fail_outbox(row_id, attempt, time.time(),
                                       'Say source message vanished from inbox', 0, True)
                return
            original = json.loads(job['raw_json'])
            message_id = self.transport.reply(original, outgoing['body'], outgoing['uuid'],
                                              before_send=self.send_guard(outgoing))
        else:
            if not chat_id:
                self.store.fail_outbox(row_id, attempt, time.time(),
                                       'Proactive say has no target chat', 0, True)
                return
            # 主动消息没有 reply 的发射前钩子：自己校验租约并打 dispatch 标记。
            self.send_guard(outgoing)()
            message_id = self.transport.send_text(chat_id, outgoing['body'],
                                                  dedupe_uuid=outgoing['uuid'])
        if not self.store.outbox_marked(row_id, attempt, time.time(), message_id):
            logging.error('outbox_marked rejected row=%s attempt=%s', row_id, attempt)
        logging.info('say sent row=%s -> %s', row_id, message_id)

    def send_retract_row(self, outgoing):
        """kind='retract'：撤回本桥发出去的那条。撤回失败只落 needs_attention，绝不回一句解释
        （那会再排一条出站，撤回失败 → 再失败 → 递归）。"""
        row_id, attempt = outgoing['row_id'], outgoing['attempt_id']
        target_message_id = outgoing.get('target_mid')
        if not target_message_id:
            self.store.fail_outbox(row_id, attempt, time.time(),
                                   'Retract row has no target message', 0, True)
            return
        target = self.store.get_outbox_by_message_id(target_message_id)
        if target is None:
            self.store.fail_outbox(row_id, attempt, time.time(),
                                   'Retract target is not a message this bridge sent', 0, True)
            return
        self.send_guard(outgoing)()
        self.transport.delete_message(target_message_id)
        if not self.store.outbox_retracted(row_id, attempt, time.time(),
                                           target_row_id=target['row_id']):
            logging.error('outbox_retracted rejected row=%s attempt=%s', row_id, attempt)
        logging.info('retract sent row=%s -> %s', row_id, target_message_id)

    def send_guard(self, outgoing):
        def validate():
            now = time.time()
            if self.stop.is_set() or not self.authenticated.is_set() or not self.enabled():
                raise TransportError('Sending paused/stopping or authentication unavailable', 'send_guard')
            row_id = outgoing['row_id']
            current = self.store.get_outbox(row_id)
            # P1-01: the 3300s idempotency window only applies once a request was actually
            # dispatched, so a pre-dispatch abort can never turn into outcome_unknown.
            if (not current or current['state'] != 'send_started'
                    or current['attempt_id'] != outgoing['attempt_id']
                    or not current['lease_until'] or current['lease_until'] - now <= 36
                    # P1-01 / S1：3300s 幂等窗口只对 reply 有意义——say/retract 没有回读校验，
                    # 也没有被飞书当成重复投递的风险（uuid 已经是幂等键）。
                    or (current['kind'] == 'reply' and current['first_send_at'] is not None
                        and now - current['first_send_at'] >= 3300)):
                raise TransportError('Stale send attempt or idempotency safety window expired', 'send_guard')
            # Stamp dispatch right before the real subprocess request starts.
            if not self.store.mark_send_dispatched(row_id, outgoing['attempt_id'], now):
                raise TransportError('Stale send attempt; dispatch marker was rejected', 'send_guard')
        return validate

    def self_channel_waited_s(self):
        """从第一次尝试认本人会话到现在过了几秒（没试过就是 None）。"""
        first = self.store.meta('self_channel_first_try_ms')
        if not isinstance(first, int):
            return None
        return max(0.0, time.time() - first / 1000.0)

    def teach_channel_status(self):
        """教材私聊的采集状态：它和群消息是同一个管道，所以健康度也要一起报出来。

        没有这个字段时，"教学看着开着但私聊没人回"只能靠人翻日志（HISTORY §10.2 的 ④）。
        """
        chat_id = teach_log.enabled_chat(self.home)
        if not chat_id:
            # 没写过配置 = 默认开通，所以"没开"必须带原因（缺权限 / 还没认出来 / 明确关掉）。
            config = teach_log.read_config(self.home)
            try:
                scopes = self.transport.user_scopes()
            except Exception:  # noqa: BLE001 - 读不到授权时只影响"为什么没开"，不该让状态接口挂掉
                scopes = None
            reason = teach_log.channel_reason(config, scopes, self.self_channel_waited_s())
            status = {'enabled': False, 'reason': reason}
            if reason in ('detecting', 'no_p2p_scope'):
                status['default_on'] = True
            return status
        epoch = self.store.meta('service_epoch_ms') or 0
        # 不报 scan.active：一个窗口搜完它就是 0，那是正常节奏不是故障（健康看落后多少秒）。
        return {'enabled': True,
                'watermark_lag_s': self.collector_watermark_lag('self', epoch, time.time())}

    def _pending_approval_status(self):
        """等批的卡有几张、最久等了多久：status/doctor 要能看见"活卡在等人"。"""
        try:
            cards = self.pending_cards()
        except Exception:  # noqa: BLE001 - 状态写不进去时退化成空，不阻塞心跳
            return {'count': 0, 'oldest_seconds': 0}
        now = time.time()
        oldest = 0.0
        for task in cards:
            try:
                oldest = max(oldest, now - float(task.get('created_at') or now))
            except (TypeError, ValueError):
                continue
        return {'count': len(cards), 'oldest_seconds': int(oldest)}

    def write_status(self):
        self.store.recover(time.time())
        with self.health_lock:
            health = json.loads(json.dumps(self.health))
        state = self.hold_state()
        active = bool(state.get('active'))
        health.update(pid=os.getpid(), heartbeat_at=time.time(), authenticated=self.authenticated.is_set(),
                      sending_enabled=self.enabled(), identity=self.identity,
                      workers={t.name: t.is_alive() for t in self.threads},
                      external_mode=True,
                      stats=self.store.stats(), service_epoch_ms=self.store.meta('service_epoch_ms'),
                      running_release=running_release(),
                      # 挂起不是故障：mode 让 status/doctor 能区分"下班"与"坏了"。
                      mode='held' if active else 'running',
                      held=active,
                      held_since=state.get('since_ms'),
                      held_by=state.get('by'),
                      held_reason=state.get('reason'),
                      last_release_at=state.get('last_release_at_ms'),
                      dropped_during_hold=self.store.meta('hold_dropped_current') or 0,
                      dropped_total=self.store.meta('hold_dropped_total') or 0,
                      pending_approval=self._pending_approval_status(),
                      control_channel='polling' if self_chat_ids(self.config) else 'unconfigured',
                      teach_channel=self.teach_channel_status())
        atomic_json(self.home / 'status.json', health)

    def fire_reminders(self):
        """把到点的提醒发出去：一次一条，抢到才发。

        ``claim_reminder`` 把 pending 改成 sending，所以两个进程同时看到同一条也只会发一次；
        发失败就退回 pending 让下一拍再试，连续失败到 3 次才放弃（并留下 last_error）。
        """
        from . import remind as reminder

        now_ms = int(time.time() * 1000)
        for row in self.store.due_reminders(now_ms):
            claimed = self.store.claim_reminder(row['id'])
            if claimed is None:
                continue
            try:
                # 提醒也是"agent 说的话"，必须带署名：否则本人会以为这条是他自己发的。
                # 走 render_reply 而不是自己拼署名，署名与脱敏只有那一份实现。
                # 提醒不 @ 任何人（含本人）：正文里的 @ 会被这里删掉，占位符也不会漏出去。
                body = render_reply(reminder.body_for(claimed, now_ms),
                                    self.config.get('signature'),
                                    owner=owner_name_at(self.home),
                                    self_open_id=self.config.get('target_open_id'))
                mid = self.transport.send_text(claimed['chat_id'], body)
            except Exception as error:  # noqa: BLE001 - 发不出去不能拖垮心跳
                attempts = int(claimed.get('attempts') or 1)
                retry = attempts < 3
                self.store.finish_reminder(claimed['id'], 'pending' if retry else 'failed',
                                           error='%s: %s' % (type(error).__name__, str(error)[:300]))
                logging.warning('reminder %s send failed (attempt %d): %s', claimed['id'],
                                attempts, error)
                continue
            self.store.finish_reminder(claimed['id'], 'sent', fired_at_ms=now_ms)
            logging.info('reminder %s sent as %s', claimed['id'], mid)

    def close(self, timeout=5.0):
        """停 → join → **断言线程真的退出** → 关库。幂等。

        顺序不能反：库被关掉时若还有 worker 在跑 SQL，轻则 ProgrammingError，
        重则（共用连接时）损坏 libsqlite3 的堆。所以先停、再等、确认死了才关。

        ``timeout`` 是**总预算**（不是每个线程各一份）：launchd 的退出超时只有 60 秒，
        四个线程各等 15 秒就会在关库中途被 SIGKILL。
        """
        self.stop.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = [thread.name for thread in self.threads if thread.is_alive()]
        self.store.close()
        if alive:
            raise RuntimeError('worker threads still alive at shutdown: %s' % ', '.join(alive))

    def run(self):
        # 上一次进程如果死在"抢到提醒、还没标记发出"中间，这条会卡在 sending：放回 pending 再跑。
        try:
            recovered = self.store.recover_reminders()
            if recovered:
                logging.warning('recovered %d reminder(s) stuck in sending', recovered)
        except Exception:  # noqa: BLE001 - 恢复失败不能阻止服务起来
            logging.exception('reminder recovery failed')
        for name in WORKER_NAMES:
            thread = threading.Thread(target=getattr(self, name), name=name, daemon=True)
            self.threads.append(thread)
            thread.start()
        heartbeat = max(0.01, self.config_seconds('heartbeat_seconds', 3))
        try:
            while not self.stop.wait(heartbeat):
                try:
                    if not self.held():
                        self.store.expire_external(time.time(), self.external_reply_timeout())
                        self.store.emit_attention_events(time.time())
                        self.store.recover_tasks(time.time(), self.external_reply_timeout())
                        self.expire_task_cards(time.time())
                        # 挂起期间不响：恢复后这一批会带着"晚了多少分钟"补发（见 remind.body_for）。
                        self.fire_reminders()
                except Exception:
                    logging.exception('external event sweep failed; service continues')
                try:
                    self.write_status()
                except Exception:
                    # P2-10: one failed status write (disk full, torn config) must not crash-loop
                    # the whole service; keep recovering queues and try again on the next beat.
                    logging.exception('status cycle failed; service continues')
                    self.stop.wait(min(3, heartbeat))
        finally:
            self.close(timeout=40)


def foreground(home):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    lock = (home / 'service.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('A bridge process already owns this state directory')
    handler = RotatingFileHandler(str(home / 'bridge.log'), maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads((home / 'config.json').read_text())
    service = Service(home, config)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: service.stop.set())
    logging.info('service starting pid=%s', os.getpid())
    try:
        service.run()
    finally:
        logging.info('service stopped')
        lock.close()
