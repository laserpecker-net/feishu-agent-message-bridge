#!/usr/bin/env python3
"""Shared, copyable Feishu event-consumer protocol runtime (POSIX Python 3.9+).

Copy this file together with ``consumer_safety.py``. The runtime owns the bridge
protocol (claim / journal / reply / fail / recovery) and the last-line safety
guard: every answer passes ``consumer_safety.finalize_answer`` before it is
persisted and submitted, so key or password material never reaches Feishu.

Answer contract: your ``build_agent()`` callable returns the final reply text and
this runtime sends that string verbatim (it never parses it), so parsing is the
adapter's job. If the underlying agent replies with a JSON object
``{"answer": "..."}``, do not parse it by hand — call
``consumer_safety.finalize_reply()`` (or ``extract_answer()``), which accepts a bare
object, a fenced code block, or prose followed by the object. It fails closed on an
unpaired brace, on a reply over 256 KiB unless the whole text is one JSON object, on
two different outermost ``answer`` values (ambiguous JSON must not decide what gets
sent), and on a missing, non-string or blank answer.

Escalation: the same JSON may carry ``"ask_owner": {"question": "…", "needs":
"fact|decision|access"}``. That is the Agent saying "I cannot answer this without the
owner", and it is delivered to the owner as a **private** message (``feishu-bridge
notify``) right after the in-thread reply, never instead of it. The runtime also
sends a private notice when it gives up on an event (timeout / adapter failure), so
"nobody ever knew" stops being a failure mode. Both go through the bridge's dedupe
and hourly cap (``bridge/notify.py``), so a burst of 15 mentions cannot turn into 15
private messages.

Task ledger (L1): the event payload may also carry ``task`` (this message started
one) and ``tasks`` (the chat's compact ledgers, newest first). The answer JSON may
carry ``"task": {"id", "state", "note", "artifacts", "open_question"}``; the runtime
forwards the normalised update as ``--task-*`` flags on ``events reply`` and journals
it next to ``ask_owner``, so a crash-recovery replay submits the same update (the
bridge is idempotent by event identity). A task update never triggers a private
message of its own: only ``ask_owner`` does.

Native Windows is rejected before any event is claimed (POSIX flock, process
groups): run this consumer in WSL2/Linux or macOS, or use the HTTP gateway with
skills/feishu-bridge-consumer-remote.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

try:  # POSIX-only runtime; native Windows is rejected before claiming anything.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on native Windows
    fcntl = None

from consumer_safety import capabilities_report, finalize_reply, require_posix_runtime


POLL_SECONDS = 5
LEASE_SECONDS = 600
# The Agent adapter must enforce its own timeout and cancel child processes.
AGENT_TIMEOUT_SECONDS = 240
COMMAND_TIMEOUT_SECONDS = 20
WORKSPACE_ENV = 'FEISHU_CONSUMER_WORKSPACE'
#: 收到之后多久还没答完，就先应一声（快问快答不打扰：15 秒内答完就一句话都不多发）。
SLOW_ACK_SECONDS = 15.0
#: 再久一点报一次进度（**只报一次**，不刷屏）：人至少知道它卡在哪，而不是一片安静。
SLOW_PROGRESS_SECONDS = 180.0
#: 两句都走 outbox（唯一出口），文案是人话（禁系统词，见 bridge/report.speakable 同一份表）。
SLOW_ACK_TEXT = '收到，我先看一下，稍等'
SLOW_PROGRESS_TEXT = '还在看，卡在「%s」，有结果我马上回'
SLOW_PROGRESS_FALLBACK = '等模型回话'


def _bridge_workbench():
    """延迟导入：`bridge` 只在跑回话端时存在（模板复制出去时不必须）。"""
    try:
        from bridge import workbench
    except ImportError:  # pragma: no cover - 模板复制场景
        return None
    return workbench


class SlowNotice:
    """答得慢的时候：先应一声，再报一次进度。

    为什么要有它：2026-11-19 真机，主人 17:26 发消息、17:30 才收到回复，中间群里四分钟
    一片安静——他以为桥挂了。事实是它在真查东西。**沉默和死亡在群里长得一模一样。**

    两条边界（都是为了"别吵"）：

    * 15 秒内答完 → 一个多余的字都不发（快问快答不能被"收到"污染）；
    * 只报一次进度，不循环刷屏；reply 已经进 outbox 之后绝不再补话。
    """

    def __init__(self, call, name, event, *, ack_seconds=None, progress_seconds=None,
                 text_ack=None, text_progress=None):
        self.call = call
        self.name = name
        self.event = event
        self.ack_seconds = SLOW_ACK_SECONDS if ack_seconds is None else ack_seconds
        self.progress_seconds = SLOW_PROGRESS_SECONDS if progress_seconds is None else progress_seconds
        self.text_ack = SLOW_ACK_TEXT if text_ack is None else text_ack
        self.text_progress = SLOW_PROGRESS_TEXT if text_progress is None else text_progress
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        self.inbox_id = payload.get('message_id')
        self._done = threading.Event()
        self._thread = None

    # ---- 生命周期 -------------------------------------------------------
    def start(self):
        if not isinstance(self.inbox_id, str) or not self.inbox_id:
            return self  # 没有来源消息就不发：挂不到原消息下的"收到"没有意义
        if self.ack_seconds <= 0:
            return self
        self._thread = threading.Thread(target=self._run, name='slow-notice', daemon=True)
        self._thread.start()
        return self

    def finished(self):
        """答完了（或者放弃了）：取消后续所有提醒，并**等线程真的退出**（不留线程泄漏）。"""
        self._done.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        return not (thread is not None and thread.is_alive())

    # ---- 内部 -----------------------------------------------------------
    def _wait(self, seconds):
        """等 seconds；返回 True 表示"到点了还没答完"。"""
        return not self._done.wait(max(0.0, seconds))

    def _run(self):
        if not self._wait(self.ack_seconds):
            return
        if not self._say('ack:%s' % self.inbox_id, self.text_ack):
            return
        if not self._wait(max(0.0, self.progress_seconds - self.ack_seconds)):
            return
        workbench = _bridge_workbench()
        step = workbench.last_step_text(SLOW_PROGRESS_FALLBACK) if workbench else SLOW_PROGRESS_FALLBACK
        self._say('progress:%s' % self.inbox_id, self.text_progress % step)

    def _say(self, external_uuid, text):
        """发一条 `say`（走 outbox）。发不出去只返回 False，绝不抛给主流程。"""
        try:
            result = self.call(['say', '--consumer', self.name, '--event', str(self.event['id']),
                                '--claim-token', self.event['claim_token'],
                                '--uuid', external_uuid], body=text)
        except Exception:  # noqa: BLE001 - 提醒失败不能连累正在生成的那条回复
            return False
        return isinstance(result, dict) and result.get('ok') is True

WORKSPACE_ENV = 'FEISHU_CONSUMER_WORKSPACE'


def default_home():
    """Works inside the bridge repo or as a copied template."""
    try:
        from bridge import host
        return host.default_home()
    except Exception:
        if sys.platform == 'darwin':
            return Path.home() / 'Library/Application Support/FeishuPersonalAgent'
        return Path.home() / '.feishu-personal-agent'


def resolve_workspace(value):
    """Validate the optional Agent workspace; the agent may search, execute and edit there."""
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit('--workspace 必须是绝对路径：%s' % value)
    if not path.is_dir():
        raise SystemExit('--workspace 目录不存在：%s' % path)
    return str(path.resolve())


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(temporary), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path):
    """读一个 JSON 对象文件；缺失/坏 JSON/非对象 → None（调用方各自决定失败语义）。"""
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_config(home):
    """读 bridge 的 config.json；读不到/不是对象返回 None（调用方各自决定失败语义）。"""
    return _read_json(Path(home) / 'config.json')


def owner_name_at(home):
    """这台机器**当前登录的飞书账号**姓名（正文自称用）；取不到返回 None → 中性自称。

    唯一的身份来源是 bridge 写在 `status.json` 里的 `identity`（bridge 用 `target_open_id`
    校验过、每轮心跳刷新）。**读法只在 `agent_prompt.owner_name_from_status` 一处实现**，
    这里只是消费者侧的入口名——bridge 的回复署名（`{owner}` 占位符）用的是同一份实现，
    两边不会各读一份，也就不会出现"正文对了、署名没对"的第二次分叉。
    """
    from agent_prompt import owner_name_from_status
    return owner_name_from_status(home)


def runtime_gate(home, identity):
    """判断"现在该不该继续领事件"，返回原因字符串或 None。

    这三个门禁原先在 `bridge/consumer.py:Consumer.enabled()` 与 service 的持有状态里各写一份；
    引擎 A 退役后统一在这里，任何适配器（cursor/dsh/codex/agentkit）都走同一条路：

    * ``sending_disabled``：`feishu-bridge pause` 之后不领事件（旧引擎的语义，别丢）。
    * ``identity_changed``：进程启动后 `target_open_id` 被改过 → 立刻停手，避免"继续用旧身份回复"。
    * ``held``：`feishu-bridge hold` 期间一概不领（挂起语义是"置之不问"，不是"排队等恢复"）。
    * ``config_unreadable``：配置读不到时失败关闭（宁可不动，也不拿默认值去回消息）。
    """
    config = read_config(home)
    if config is None:
        return 'config_unreadable'
    if not config.get('sending_enabled'):
        return 'sending_disabled'
    if config.get('target_open_id') != identity:
        return 'identity_changed'
    try:
        hold = json.loads((Path(home) / 'hold.json').read_text())
    except (OSError, ValueError):
        hold = {}
    if isinstance(hold, dict) and hold.get('active'):
        return 'held'
    return None


def identity_at_start(home):
    """进程启动时的目标身份；之后 config 里变了就视为"被改过"。"""
    try:
        return json.loads((Path(home) / 'config.json').read_text()).get('target_open_id')
    except (OSError, ValueError):
        return None


#: bridge 的顶层命令（会被拼成 `feishu-bridge <cmd> --home …` 而不是 `events <cmd>`）。
#: 漏一个就会拼成 `events remind add` 并以非零退出，
#: 表现是"回复说提醒设好了、库里一条没有"——同一个坑踩过两次（2026-11-06 的 teach add、同日的
#: remind add），所以 `tests/test_consumer_runtime.py` 钉住这张表与运行时会发的命令一致。
TOP_LEVEL_COMMANDS = ('notify', 'teach', 'remind', 'task')


def command(binary, home, args, body=None):
    # `notify` 与 `teach` 是 bridge 的顶层命令（不是 events 子命令）；两个入口共用这一个 callable，
    # 适配器和运行时才能用同一份超时/错误处理去发私聊、写教材。
    # 2026-11-06 踩过：只把 notify 放进白名单，`teach add` 被拼成 `events teach add`，
    # 每次都以非零退出静默失败——模型说"记下了"，教材一个字没写。
    head = [] if args and args[0] in TOP_LEVEL_COMMANDS else ['events']
    argv = [str(binary)] + head + ['--home', str(home)] + args
    result = subprocess.run(argv, input=body, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=COMMAND_TIMEOUT_SECONDS,
                            stdin=None if body is not None else subprocess.DEVNULL)
    if result.returncode:
        # Never log stderr: an external command may echo message content.
        raise RuntimeError('Bridge event command failed')
    response = json.loads(result.stdout)
    if not isinstance(response, dict):
        raise ValueError('Invalid bridge response')
    return response


def current_event(call, name, event):
    """Never use an old claim token after another consumer has reclaimed it."""
    listing = call(['list', '--consumer', name, '--limit', '200'])
    for row in listing.get('events', []):
        if (row.get('id') == event['id'] and row.get('consumer') == name and
                row.get('claim_token') == event['claim_token']):
            if row.get('state') == 'done':
                return row
            if row.get('state') == 'leased' and row.get('lease_until', 0) > time.time():
                return row
    return None  # Bounded lookup fails closed.


def attempt_path(state, event):
    event_id, token = event['id'], event['claim_token']
    if (isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0 or
            not isinstance(token, str) or len(token) != 32 or
            any(char not in '0123456789abcdef' for char in token)):
        raise ValueError('Invalid event identity')
    return state / str(event_id) / token / 'attempt.json'


def remaining_seconds(call, event, current):
    """这条事件还剩多少秒可用：外部回复超时窗口与租约的较小者。

    以前这里还看 ``external.enabled``（"外部接管开关"）：开关删掉后消费者不能再读它，
    否则每条消息都会算出 0 秒 → 立刻以 ``InsufficientTime`` 失败（真机踩过，HISTORY §14）。
    现在只有一个事实：``reply_timeout_seconds``。
    """
    external = call(['status']).get('external', {})
    timeout = external.get('reply_timeout_seconds')
    if (isinstance(timeout, bool) or not isinstance(timeout, int) or
            not 60 <= timeout <= 86400):
        raise ValueError('Invalid external reply timeout')
    return min(float(event['created_at']) + timeout,
               float(current['lease_until'])) - time.time()


def escalation_body(ask, answer, payload):
    """本人私聊里看到的求助正文：问题一句话在前，上下文和"我先回了什么"在后。

    收件人是本人，不是提问方——所以他需要的是"谁问了什么、卡在哪、我回了什么"，
    而不是完整的对话抄送。
    """
    text = payload.get('text') if isinstance(payload, dict) else ''
    excerpt = ' '.join(str(text or '').split())[:200]
    said = ' '.join(str(answer or '').split())[:200]
    label = {'fact': '缺事实', 'decision': '缺决定', 'access': '缺权限',
             'ambiguity': '指令不清楚', 'uncertainty': '我拿不准'}.get(ask.get('needs'), '需要你确认')
    return '\n'.join(['【需要你出手】' + ask['question'], '',
                      '· 有人问到：' + (excerpt or '（消息无正文）'),
                      '· 我先回的：' + (said or '（没回）'),
                      '· 类型：' + label, '',
                      '要教我下次怎么办：**引用回复我这条**，写 A/B/C 或你自己的话。'
                      '（没引用我分不清你在答哪一条，30 分钟后这条就作废，不影响别的消息。）'])


def _action(call, argv, failure, fallback):
    """跑一个写盘动作，把"调用炸了"和"命令说没成"都折成 {ok: False, reason}。

    四个动作（记教材/撤教材/设提醒/撤提醒）的差别只有命令与失败说法，流程本身一模一样；
    分开写四遍的话，以后加第五个动作就会有人漏掉某种失败路径。
    """
    try:
        result = call(argv)
    except Exception:  # noqa: BLE001 - 写盘失败不能弄坏回复，原因进回执
        return {'ok': False, 'reason': failure}
    if not isinstance(result, dict) or result.get('ok') is not True:
        reason = result.get('reason') if isinstance(result, dict) else None
        return {'ok': False, 'reason': reason or fallback}
    return result


def add_teaching(call, event, teach):
    """把"模型判出这是教材"落盘（`teach add`，按来源消息 id 幂等）。

    为什么由运行时调 CLI 而不是让模型自己敲命令：写盘规矩（类型、长度、条数上限、幂等）
    只能有一份实现，在 `bridge/teach.py` 里；模型只提供 kind 与正文。
    返回值直接进回执：成功带条目号与档位，失败带原因——回执由这里说，模型正文不说。
    """
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    message_id = payload.get('message_id') or event.get('dedupe_key')
    if not isinstance(message_id, str) or not message_id.startswith('om_'):
        return {'ok': False, 'reason': '没有来源消息 id，写不了'}
    argv = ['teach', 'add', '--kind', teach['kind'], '--text', teach['text'],
            '--message-id', message_id]
    if teach.get('name'):
        argv += ['--name', teach['name']]
    return _action(call, argv, '写入命令失败', '写入失败')


def revert_teaching(call, event, target):
    """撤回一条教材（`teach revert <id>` 或 `--last`），结果进回执。"""
    argv = ['teach', 'revert', '--last'] if target == 'last' else ['teach', 'revert', str(target)]
    return _action(call, argv, '撤回命令失败', '撤回失败')


def set_reminder(call, event, remind):
    """登记一条提醒（`remind add`，按来源消息 id 幂等），结果进回执。

    提醒落在 bridge 的库里、由服务心跳发出，而不是"在这个会话里睡一觉再发"：
    消费者会话随时会超时/重启，睡在进程里的定时器一定丢。
    """
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    chat_id = payload.get('chat_id')
    message_id = payload.get('message_id') or event.get('dedupe_key')
    if not isinstance(chat_id, str) or not chat_id:
        return {'ok': False, 'reason': '这条消息没有会话 id，发不了提醒'}
    if not isinstance(message_id, str) or not message_id.startswith('om_'):
        return {'ok': False, 'reason': '没有来源消息 id，设不了提醒'}
    argv = ['remind', 'add', '--at', remind['at'], '--text', remind['text'],
            '--chat', chat_id, '--message-id', message_id]
    if remind.get('name'):
        argv += ['--name', remind['name']]
    return _action(call, argv, '提醒命令失败', '提醒没设上')


def cancel_reminder(call, event, key):
    """取消一条还没发的提醒（`remind cancel <名称或编号>`），结果进回执。"""
    return _action(call, ['remind', 'cancel', str(key)], '取消命令失败', '没有可取消的提醒')


def _clip(text, limit=40):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def action_receipts(record):
    """把这条消息真正做了什么，翻译成本人看得懂的几行（追加在回复正文末尾）。

    四要素里有两项只有写盘之后才知道：记在哪（档位 + 条目号）、怎么撤回（口令带条目号）。
    所以成败都由这几行说，模型正文不再承诺"记下了"——两处都说的结果，必有一处是假话。
    """
    lines = []
    dropped = record.get('dropped') or []
    if 'teach' in dropped:
        lines.append('· 没记上：判断出的记忆类型或正文不合规 —— 这句没生效。')
    elif record.get('teach'):
        teach_result = record.get('teach_result') or {}
        entry_id = teach_result.get('id')
        if teach_result.get('ok') and entry_id:
            name = teach_result.get('name') or entry_id
            lines.append('· 记在「%s」· %s ｜ 觉得不对，照平时那样 @ 我，说「撤回 %s」'
                         % (teach_result.get('label') or '记忆', name, name))
        else:
            lines.append('· 没记上：%s —— 这句没生效，要重试就把这句话再发一遍。'
                         % _clip(teach_result.get('reason') or '写入失败', 60))
    if 'remind' in dropped:
        lines.append('· 提醒没设上：时间或内容没看懂 —— 这句没生效。')
    elif record.get('remind'):
        remind_result = record.get('remind_result') or {}
        if remind_result.get('ok'):
            lines.append('· 提醒设好了：%s（%s）｜ 不想提醒了，@ 我说「取消 %s」'
                         % (remind_result.get('fire_at') or '到点',
                            remind_result.get('when') or '',
                            remind_result.get('name') or _clip(remind_result.get('text'))))
        else:
            lines.append('· 提醒没设上：%s —— 要设就再说了个准确的时间。'
                         % (remind_result.get('reason') or '原因不明'))
    if 'remind_cancel' in dropped:
        lines.append('· 没取消：要取消哪条提醒我没看懂。')
    elif record.get('remind_cancel'):
        cancel_result = record.get('cancel_result') or {}
        if cancel_result.get('ok'):
            lines.append('· 提醒撤了：「%s」（原定 %s）—— 到点不响了。'
                         % (cancel_result.get('name') or _clip(cancel_result.get('text')),
                            cancel_result.get('fire_at') or '某个时间'))
        else:
            lines.append('· 没撤掉：%s' % (cancel_result.get('reason') or '原因不明'))
    if 'direct_work' in dropped:
        lines.append('· 没派出去：五段没写全（缺要动什么或怎么算做完）—— 这件事没发车。')
    elif record.get('direct_work'):
        direct_result = record.get('direct_work_result') or {}
        if direct_result.get('ok'):
            lines.append('· 已派给干活端：它在独立工作树里干，出结果会回群里。')
        else:
            lines.append('· 没派出去：%s —— 这件事还悬着，别当成已开始。'
                         % _clip(direct_result.get('error') or '原因不明', 60))
    if 'task_card' in dropped:
        lines.append('· 卡片没起草：五段没写全或步骤过多 —— 这件事没发去请人批。')
    elif record.get('task_card'):
        card_result = record.get('task_card_result') or {}
        if card_result.get('ok'):
            lines.append('· 任务卡已发你私聊：你批了我才动。')
        else:
            lines.append('· 卡没发出去：%s —— 这件事没人批，先不动。'
                         % _clip(card_result.get('error') or '原因不明', 60))
    if 'teach_revert' in dropped:
        lines.append('· 没撤掉：撤回目标不合规 —— 那条记忆还在生效。')
    elif record.get('teach_revert'):
        revert_result = record.get('revert_result') or {}
        if revert_result.get('ok') and revert_result.get('id'):
            lines.append('· 撤了「%s」（%s：%s）—— 以后不按它办。'
                         % (revert_result.get('name') or revert_result['id'],
                            revert_result.get('label') or '记忆', _clip(revert_result.get('text'))))
        else:
            lines.append('· 没撤掉：%s' % _clip(revert_result.get('reason') or '那条记忆还在生效', 60))
    return lines


def escalate(call, event, ask, answer):
    """把求助发给本人私聊；同一事件只发一次（去重键 = 事件 id）。"""
    return call(['notify', '--dedupe-key', 'event:%s' % event['id'],
                 '--question-event', str(event['id']),
                 '--needs', str(ask.get('needs') or 'fact')],
                body=escalation_body(ask, answer, event.get('payload')))


def dispatch_direct(call, event, work):
    """把答复里的 ``direct_work`` 交给桥：建账 + 按 owner_direct 批准 + 群里回执（不发卡）。"""
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    chat_id = payload.get('chat_id')
    mid = payload.get('message_id')
    if not isinstance(chat_id, str) or not chat_id or not isinstance(mid, str) or not mid:
        return {'ok': False, 'error': 'payload has no chat_id/message_id'}
    try:
        return call(['task', 'direct', '--chat', chat_id, '--origin-mid', mid],
                    body=json.dumps(work, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - 派活失败不能连累已发出的回复
        return {'ok': False, 'error': '%s: %s' % (type(error).__name__, str(error)[:200])}


def draft_card(call, event, card):
    """把答复里的 ``task_card`` 交给桥去起草：建账 + 私聊卡片 + 登记待答。

    为什么在"回复已提交"之后做：正文是主人/群友马上要看到的东西，卡片只是请求批准；
    起草失败最多是"这件事没请他批"，绝不能让已经写好的回复作废。失败记进 journal，
    不重试（重试会在私聊里发第二张卡）——真没发出去，回复末尾的 ``dropped`` 已经说了。
    """
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    chat_id = payload.get('chat_id')
    mid = payload.get('message_id')
    if not isinstance(chat_id, str) or not chat_id or not isinstance(mid, str) or not mid:
        return {'ok': False, 'error': 'payload has no chat_id/message_id'}
    try:
        return call(['task', 'draft', '--chat', chat_id, '--origin-mid', mid,
                     '--question-event', str(event['id'])],
                    body=json.dumps(card, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - 起草失败不能连累已发出的回复
        return {'ok': False, 'error': '%s: %s' % (type(error).__name__, str(error)[:200])}


def notice_failure(call, event, reason):
    """答不上来时提醒本人；这类通知是尽力而为，失败不能影响 fail 本身。"""
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    excerpt = ' '.join(str(payload.get('text') or '').split())[:200]
    # 教材私聊里没有第二套规则兜底了（2026-11-06）：判读只有模型这一条路，失败就把话说清楚，
    # 让他重说一遍——bridge 不再替他"猜一个候选"塞进槽里等确认。
    if payload.get('self_intent') == 'infer':
        hint = '把这句话再发一遍就行（这次重新判）。'
    else:
        hint = '要给我提示：**引用回复我这条**，或者直接在群里回那条消息；我拿到你说的再处理。'
    body = '\n'.join(['【没能答上】有人 @ 你，这条我没答完（' + reason + '）。', '',
                       '· 原话：' + (excerpt or '（消息无正文）'), '', hint])
    try:
        call(['notify', '--dedupe-key', 'fail:%s:%s' % (event['id'], reason)], body=body)
        return True
    except Exception:  # noqa: BLE001 - 通知失败绝不能让事件本身卡住
        return False


def reply_arguments(name, event, task):
    """拼 `events reply` 的参数；台账更新只带真的出现了的开关。

    字段的取舍与校验已经在 ``consumer_safety.normalize_task_update`` 里做完，这里只负责
    "有就带上、没有就不带"，不再解释一遍语义——同一件事有两处判断，早晚会分叉。
    """
    argv = ['reply', '--consumer', name, '--event', str(event['id']),
            '--claim-token', event['claim_token']]
    if not isinstance(task, dict):
        return argv
    for key, flag in (('id', '--task-id'), ('state', '--task-state'),
                      ('note', '--task-note'), ('open_question', '--task-question')):
        value = task.get(key)
        if isinstance(value, str) and value:
            argv += [flag, value]
    artifacts = task.get('artifacts')
    if isinstance(artifacts, list) and artifacts:
        argv += ['--task-artifacts-json', json.dumps(artifacts, ensure_ascii=False)]
    return argv


def process(call, name, state, event, generate, stop_event):
    """处理一条事件：外面只包一层"答得慢就先应一声"，其余逻辑原样在 ``_process``。

    提醒的生命周期必须包住整个处理（包括失败、放弃、异常退出）：线程在 ``finally`` 里
    一定被收掉——``tests/conftest.py`` 会把任何残留线程判红，这是本仓的线程纪律。
    """
    notice = SlowNotice(call, name, event).start()
    try:
        return _process(call, name, state, event, generate, stop_event, notice)
    finally:
        notice.finished()


def _process(call, name, state, event, generate, stop_event, notice=None):
    path = attempt_path(state, event)
    if path.exists():
        record = json.loads(path.read_text())
        if record.get('event_id') != event['id'] or record.get('claim_token') != event['claim_token']:
            raise ValueError('Journal identity mismatch')
    else:
        record = {'event_id': event['id'], 'claim_token': event['claim_token']}
        atomic_json(path, record)
    if record.get('receipt') or record.get('abandoned'):
        return
    current = current_event(call, name, event)
    if current is None:
        record['abandoned'] = True
        atomic_json(path, record)
        return
    if 'answer' not in record:
        if current['state'] == 'done':
            record['abandoned'] = True
            atomic_json(path, record)
            return
        if stop_event.is_set():
            return  # Keep this live claim for restart; do not mark it abandoned.
        remaining = remaining_seconds(call, event, current)
        if (current['state'] != 'leased' or remaining <= AGENT_TIMEOUT_SECONDS +
                3 * COMMAND_TIMEOUT_SECONDS):
            call(['fail', '--consumer', name, '--event', str(event['id']),
                  '--claim-token', event['claim_token'], '--error', 'InsufficientTime'])
            record['abandoned'] = True
            atomic_json(path, record)
            notice_failure(call, event, 'InsufficientTime')
            return
        payload = event.get('payload')
        if event.get('kind') != 'message' or not isinstance(payload, dict):
            raise ValueError('Expected a message payload')
        try:
            # 只有群消息可以"不回"：私聊对本人沉默等于失联（见 finalize_reply 的 allow_skip）。
            allow_skip = payload.get('chat_type') == 'group'
            reply = finalize_reply(generate(payload, event, stop_event), allow_skip=allow_skip)
            if isinstance(payload, dict) and payload.get('self_intent') == 'infer':
                # 教材私聊里这条回复本身就是给本人看的：再发一条私聊求助只是同一件事说两遍。
                # 要问的话，模型直接写在正文里（见 agent_prompt 的判读段）。
                reply['ask_owner'] = None
        except Exception:
            if stop_event.is_set():
                return  # Adapter cancellation is not an Agent failure.
            if current_event(call, name, event) is not None:
                call(['fail', '--consumer', name, '--event', str(event['id']),
                      '--claim-token', event['claim_token'], '--error', 'AgentFailed'])
            record['abandoned'] = True
            atomic_json(path, record)
            notice_failure(call, event, 'AgentFailed')
            return
        # 正文、私聊求助和台账更新一起落盘：崩溃恢复后既不会重新生成，也不会把同一个问题
        # 问两遍，更不会漏报这一步的任务状态（reply 是幂等的，重复提交只得到 duplicate）。
        record['answer'] = reply['answer']
        if reply.get('skip'):
            record['skip'] = reply['skip']
        record['ask_owner'] = reply.get('ask_owner')
        record['task'] = reply.get('task')
        record['task_card'] = reply.get('task_card')
        record['direct_work'] = reply.get('direct_work')
        record['teach'] = reply.get('teach')
        record['teach_revert'] = reply.get('teach_revert')
        record['remind'] = reply.get('remind')
        record['remind_cancel'] = reply.get('remind_cancel')
        record['dropped'] = reply.get('dropped') or []
        atomic_json(path, record)
    if stop_event.is_set():
        return  # Saved answer is recovered after restart if this claim is still valid.
    current = current_event(call, name, event)
    if current is None:
        record['abandoned'] = True
        atomic_json(path, record)
        return
    if current['state'] == 'leased' and remaining_seconds(call, event, current) <= COMMAND_TIMEOUT_SECONDS:
        # Keep the answer; a late reply must fail closed, and no new token is borrowed.
        return
    if record.get('skip'):
        # 「这条不用回」的收尾和回复一样只有一条实现：bridge 那边把事件 done、消息记 ignored，
        # 一个消息都不发。跳过不产生回执，也不需要署名。
        results = record.get('skip_result')
        if results is None:
            results = call(['skip', '--consumer', name, '--event', str(event['id']),
                            '--claim-token', event['claim_token'],
                            '--reason', record['skip']])
            record['skip_result'] = results
            atomic_json(path, record)
        if results.get('ok') is not True:
            raise RuntimeError('Bridge did not accept the skip')
        record['receipt'] = results
        atomic_json(path, record)
        return
    # 先把动作做完、拿到条目号，再发回复：回执里的"记在哪、怎么撤"只有写盘之后才知道。
    # 反过来（先回后写）就只能在正文里承诺"记下了"，而写失败时那句已经发出去了。
    # 两个动作各自幂等（记按来源消息 id、撤按条目 id 或 last），崩在中途重放不会做两遍。
    if record.get('teach_revert') and 'revert_result' not in record:
        record['revert_result'] = revert_teaching(call, event, record['teach_revert'])
        atomic_json(path, record)
    if record.get('remind_cancel') and 'cancel_result' not in record:
        record['cancel_result'] = cancel_reminder(call, event, record['remind_cancel'])
        atomic_json(path, record)
    if record.get('teach') and 'teach_result' not in record:
        record['teach_result'] = add_teaching(call, event, record['teach'])
        atomic_json(path, record)
    # 回执单独传，不拼进正文：bridge 负责把署名放在正文和回执之间（落款不能夹在附注中间）。
    # 走文件而不是 argv：正文因为同样的理由只从 stdin/文件读，回执里有教材名称，也不该进 ps。
    if record.get('remind') and 'remind_result' not in record:
        record['remind_result'] = set_reminder(call, event, record['remind'])
        atomic_json(path, record)
    if record.get('direct_work') and 'direct_work_result' not in record:
        # 主人自己下的写活：免批准但必须走干活端。派失败不抛错（回复照发，回执里说明），
        # 否则一件"没派出去"的活会把已经写好的答复也拖下水。
        record['direct_work_result'] = dispatch_direct(call, event, record['direct_work'])
        atomic_json(path, record)
    if record.get('task_card') and 'task_card_result' not in record:
        # 和教材/提醒同理：起草结果要出现在回执里（"卡已发你私聊"），所以必须在发回复之前做完。
        # 起草失败不抛错（draft_card 自己咽下），回复照发——卡片是请求批准，不是回复本身。
        record['task_card_result'] = draft_card(call, event, record['task_card'])
        atomic_json(path, record)
    receipts = action_receipts(record)
    argv = reply_arguments(name, event, record.get('task'))
    if receipts:
        receipt_path = attempt_path(state, event).with_suffix('.receipt')
        receipt_path.write_text('\n'.join(receipts) + '\n')
        argv += ['--receipt-file', str(receipt_path)]
    receipt = call(argv, body=record['answer'])
    if receipt.get('ok') is not True:
        raise RuntimeError('Bridge did not accept the reply')
    if notice is not None:
        # 回复已经进队列：从现在起不再补"收到/还在看"，否则人先看到回执、又看到一句迟到的话。
        notice.finished()
    if record.get('ask_owner') and not record.get('escalated'):
        # 回复已经提交成功，再私聊求助；求助失败只记一笔，不能把已发出的回复算成失败。
        try:
            record['escalated'] = escalate(call, event, record['ask_owner'], record['answer'])
        except Exception:  # noqa: BLE001 - 同理：私聊是尽力而为
            record['escalated'] = {'ok': False}
    record['receipt'] = receipt  # duplicate=true is a normal idempotent receipt.
    atomic_json(path, record)
    # reply atomically marks the message event done. Do not ack it.


def main(build_agent, default_consumer=None, default_kind='other'):
    require_posix_runtime()  # before argparse/default_home touches the filesystem
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consumer', required=default_consumer is None, default=default_consumer,
                        help='Unique name for this Agent')
    parser.add_argument('--kind', choices=('dsh', 'codex', 'other'), default=default_kind)
    parser.add_argument('--home', type=Path, default=default_home())
    parser.add_argument('--workspace', default=os.environ.get(WORKSPACE_ENV) or None,
                        help='Agent 工作空间（绝对路径）：可检索、执行并修改该目录，默认用私有临时目录')
    parser.add_argument('--check', action='store_true',
                        help='Validate the Agent adapter without registering or claiming')
    args = parser.parse_args()
    workspace = resolve_workspace(args.workspace)
    if workspace:
        os.environ[WORKSPACE_ENV] = workspace
    else:
        os.environ.pop(WORKSPACE_ENV, None)
    generate = build_agent(args.home)  # Fail before register/claim until this Agent is implemented.
    if not callable(generate):
        raise TypeError('build_agent() must return a callable')
    if args.check:
        report = capabilities_report('replier')
        print('Agent adapter is callable; no bridge event was claimed%s; capabilities=%s'
              % ('; workspace=%s' % workspace if workspace else '',
                 json.dumps(report['capabilities'], ensure_ascii=False)))
        return
    binary = args.home / 'bin/feishu-bridge'
    identity = identity_at_start(args.home)
    if identity is None:
        # 没有可读的 config.json 就没有"该以谁的身份回复"：与其在门禁里空转刷日志，
        # 不如立刻失败并告诉人下一步（安装没跑、home 指错、配置文件坏了）。
        raise SystemExit('读不到 %s 里的 target_open_id：先运行 install.py 完成安装，'
                         '或用 --home 指向正确的 bridge home' % (args.home / 'config.json'))
    state = args.home / 'consumer-state' / args.consumer
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    lock_path = state / 'consumer.lock'
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if fcntl is None:  # Defensive: require_posix_runtime() already rejects native Windows.
        raise SystemExit('fcntl is required: run this consumer in WSL2/Linux or macOS')
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def call(argv, body=None):
            return command(binary, args.home, argv, body)

        call(['register', '--consumer', args.consumer, '--kind', args.kind])
        gated = None
        while not stop_event.is_set():
            reason = runtime_gate(args.home, identity)
            if reason:
                if reason != gated:  # 只在门禁切换时说一次，别刷日志
                    print('consumer gated: ' + reason, flush=True)
                    gated = reason
                stop_event.wait(POLL_SECONDS)
                continue
            gated = None
            try:
                pending = None
                for path in sorted(state.glob('*/*/attempt.json')):
                    saved = json.loads(path.read_text())
                    if not saved.get('receipt') and not saved.get('abandoned'):
                        pending = path
                        break
                if pending is not None:
                    record = json.loads(pending.read_text())
                    listing = call(['list', '--consumer', args.consumer, '--limit', '200'])
                    event = next((e for e in listing.get('events', [])
                                  if e.get('id') == record['event_id'] and
                                  e.get('claim_token') == record['claim_token']), None)
                    if event is None:
                        record['abandoned'] = True
                        atomic_json(pending, record)
                    else:
                        process(call, args.consumer, state, event, generate, stop_event)
                else:
                    # A claim may have committed even if its CLI response was lost.
                    # Recover our unjournaled lease before claiming anything new.
                    owned = call(['list', '--consumer', args.consumer, '--state', 'leased', '--limit', '200'])
                    leases = [event for event in owned.get('events', [])
                              if event.get('kind') == 'message' and event.get('state') == 'leased' and
                              event.get('consumer') == args.consumer and
                              event.get('lease_until', 0) > time.time()]
                    if leases:
                        process(call, args.consumer, state, leases[-1], generate, stop_event)
                    else:
                        result = call(['claim', '--consumer', args.consumer, '--kind', 'message',
                                       '--limit', '1', '--lease', str(LEASE_SECONDS)])
                        for event in result.get('events', []):
                            process(call, args.consumer, state, event, generate, stop_event)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                # Log only the exception type. Inspect bridge status for queue state.
                print('consumer loop error: ' + type(error).__name__, flush=True)
            if not stop_event.is_set():
                time.sleep(POLL_SECONDS)
