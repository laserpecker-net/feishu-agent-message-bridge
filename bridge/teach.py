"""教材：master 通过私聊教给 bridge agent 的准则、风格与资料指针。

存储是一份追加日志（``home/teachings.jsonl``），一行一条不可变记录，当前状态由重放得到。
不放进 SQLite：事件库有 ``PRAGMA user_version`` 门禁，加表会把教材绑死在 release 回滚上。
"""

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path

from agent_prompt import TEACHING_MAX_CHARS

SCHEMA = 1
P2P_READ_SCOPE = 'im:message.p2p_msg:get_as_user'
#: 「还没认出本人会话」等多久后改口成「你还没有自己跟自己的会话」。
#: 两种情况对用户的下一步不一样：前者什么都别做，后者要在飞书里给自己发一条消息。
SELF_CHAT_DISCOVERY_WAIT_S = 300
MAX_ENTRIES = 200
MAX_ITEM_CHARS = 500
# 注入上限属于 prompt 预算，只在 agent_prompt 定义一次；这里只是同一个数字的别名。
MAX_INJECT_CHARS = TEACHING_MAX_CHARS

CHARTER_KINDS = ('principle', 'style', 'feedback')
NOTE_KINDS = ('fact', 'pointer')
#: 「教训」= 复盘时从失败里归纳出来的一条做法。它和资料一样**按相关性检索**（不是每轮必给），
#: 但语气是"以后遇到这类事怎么办"，所以渲染时单独一段（见 agent_prompt.teaching_section）。
LESSON_KINDS = ('lesson',)
#: 参与检索的条目：资料 + 教训。准则每轮必给，不需要检索也不需要进索引。
SEARCH_KINDS = NOTE_KINDS + LESSON_KINDS
KINDS = CHARTER_KINDS + SEARCH_KINDS
EXPOSE = ('say', 'know')

KIND_LABEL = {'principle': '原则', 'style': '风格', 'feedback': '反馈',
              'fact': '事实', 'pointer': '资料', 'lesson': '教训'}
EXPOSE_LABEL = {'say': '可以直接说', 'know': '只能自己知道'}
DEFAULT_EXPOSE = {'principle': 'say', 'style': 'say', 'feedback': 'say',
                  'fact': 'know', 'pointer': 'know', 'lesson': 'say'}

PERMISSION_LINE = '· 教材只影响做法与偏好，不改变权限（删除/密钥/MCP 仍然禁止）'

#: 条目的「名称」：给本人看的短标签（记在「风格 · 汇报先给结论」）。
#: 编号（t-0007）内部用，人读起来没意义——2026-11-06 本人原话："编号读不懂，有一个名称看起来直观一些"。
MAX_NAME_CHARS = 16
_NAME_STRIP = re.compile(r'[\s「」『』《》"\'“”‘’]+')
_NAME_TAIL = re.compile(r'[，、；。！？,.!?:：]+$')


def normalize_name(value):
    """把模型/本人给的名称收成一行短标签；不合规返回 ''（调用方回退到自动取名）。"""
    if not isinstance(value, str):
        return ''
    name = _NAME_STRIP.sub(' ', value).strip()
    name = _NAME_TAIL.sub('', name).strip()
    if not name or len(name) > MAX_NAME_CHARS:
        return ''
    return name


#: 一句话里最像"名称"的那一段：第一个逗号/分号之前通常就是这条规矩的主语。
_NAME_BREAK = re.compile(r'[，、；。！？,;!?]')


def auto_name(text, limit=12):
    """没有名称时从正文里取一段当名字：不臆造，只截取本人说过的话。

    优先取第一个逗号前的整段（「以后不要称呼我为刘总，称呼我为宝哥」→「以后不要称呼我为刘总」），
    太短或太长再退回按字数截取。
    """
    body = _NAME_TAIL.sub('', ' '.join(str(text or '').split()))
    if not body:
        return ''
    if len(body) <= limit:
        return body
    head = _NAME_BREAK.split(body, 1)[0].strip()
    if 4 <= len(head) <= limit:
        return head
    return body[:limit] + '…'


def _unique_name(name, taken):
    """同名不换名、只加序号：本人看到「汇报先给结论（2）」比看到「记不上」更容易处理。"""
    if name not in taken:
        return name
    index = 2
    while '%s（%d）' % (name, index) in taken:
        index += 1
    return '%s（%d）' % (name, index)

_config_cache = {}


def config_path(home):
    return Path(home) / 'teach.json'


def log_path(home):
    return Path(home) / 'teachings.jsonl'


def lock_path(home):
    return Path(home) / 'teach.lock'


def questions_path(home):
    return Path(home) / 'questions.jsonl'


def read_config(home):
    """`home/teach.json` 的 mtime+size 缓存读。

    缺文件返回 ``{}``（教学未启用），解析失败返回 ``{'corrupt': True}``——**绝不**返回
    ``{'enabled': True}``：配置读坏时把教学打开，等于让一个不认识的文件替 master 发言。
    """
    path = config_path(home)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _config_cache.get('value')
    if cached and cached[0] == key:
        return dict(cached[1])
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            value = {'corrupt': True}
    except (OSError, ValueError):
        value = {'corrupt': True}
    _config_cache['value'] = (key, value)
    return dict(value)


def default_channel(chat_id, enabled_at_ms, auto=True):
    """私聊通道的配置内容：显式 enable 与"默认开通"写的是同一份东西。

    ``auto`` 只用于解释来路（``teach status`` 里能看出这条是服务自己认出来登记的，
    还是本人明确指定过的），不影响任何采集判定。
    """
    return {'schema': SCHEMA, 'enabled': True, 'enabled_at_ms': int(enabled_at_ms),
            'chat_id': chat_id, 'auto': bool(auto),
            'max_entries': MAX_ENTRIES, 'max_item_chars': MAX_ITEM_CHARS,
            'max_inject_chars': MAX_INJECT_CHARS}


def write_config(home, value):
    """原子写 ``home/teach.json``（tmp + rename）：半截配置比没有配置更难查。"""
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
    os.replace(temp, path)


def channel_reason(config, scopes=None, waited_s=None):
    """私聊通道现在是什么状态。

    取值：``on`` / ``disabled`` / ``corrupt`` / ``detecting`` / ``no_p2p_scope`` / ``no_self_chat``。
    服务（采集循环）和 CLI（``teach status``、``doctor``）都读这一份判定：默认开通之后，
    "没开"有多种原因，各写一套迟早会给出两个不同答案。

    ``scopes`` 为 None 表示还没读到授权范围（调用方自己决定要不要去读）。
    ``waited_s`` 是从第一次尝试认会话到现在过了多少秒：等过 ``SELF_CHAT_DISCOVERY_WAIT_S``
    还认不出来，就不是"马上就好"，而是这台账号**还没有**自己跟自己的会话——那时候让人干等
    是错的，得告诉他去飞书里给自己发一条。
    """
    if config.get('corrupt'):
        return 'corrupt'
    if config.get('enabled') is False:
        return 'disabled'
    chat_id = config.get('chat_id')
    if isinstance(chat_id, str) and chat_id.startswith('oc_'):
        return 'on'
    waited = waited_s if isinstance(waited_s, (int, float)) else None
    if waited is not None and waited >= SELF_CHAT_DISCOVERY_WAIT_S:
        return 'no_self_chat'
    if scopes is None:
        return 'detecting'
    return 'detecting' if P2P_READ_SCOPE in scopes else 'no_p2p_scope'


def enabled_chat(home):
    """教学可用时返回那个 p2p 私聊的 chat_id，否则 None。"""
    config = read_config(home)
    if config.get('corrupt') or config.get('enabled') is not True:
        return None
    chat_id = config.get('chat_id')
    if not isinstance(chat_id, str) or not chat_id.startswith('oc_'):
        return None
    return chat_id


@contextlib.contextmanager
def _lock(home):
    """跨进程写锁：CLI 与服务都可能改教材，读-改-写必须整段在锁内。"""
    path = lock_path(home)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def new_entry(kind, text, message_id, at_ms, expose=None, reply_to=None, ref=None, name=None):
    """构造一条 add 记录。``id`` 和 ``rev`` 由 ``append`` 在锁内分配（避免并发撞号）。"""
    if kind not in KINDS:
        raise ValueError('未知的教材类型 %r（只认 %s）' % (kind, '/'.join(KINDS)))
    tier = expose or DEFAULT_EXPOSE[kind]
    if tier not in EXPOSE:
        raise ValueError('未知的可见档 %r（只认 %s）' % (tier, '/'.join(EXPOSE)))
    body = ' '.join(str(text or '').split())
    if not body:
        raise ValueError('教材正文为空')
    if len(body) > MAX_ITEM_CHARS:
        raise ValueError('条目超过 %d 字，缩到 %d 字以内再教' % (MAX_ITEM_CHARS, MAX_ITEM_CHARS))
    label = normalize_name(name) or auto_name(body)
    return {'v': SCHEMA, 'op': 'add', 'kind': kind, 'expose': tier, 'text': body,
            'name': label,
            'at_ms': int(at_ms), 'message_id': message_id, 'reply_to': reply_to,
            'ref': ref if isinstance(ref, dict) else None, 'by': 'master'}


def _read_lines(path):
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return [], None
    except OSError as error:
        return [], '文件读不出来：%s' % error
    return raw.splitlines(), None


def _replay(home):
    lines, read_error = _read_lines(log_path(home))
    entries = {}
    order = []
    by_message = {}
    rev = 0
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(record, dict):
            bad += 1
            continue
        try:
            rev = max(rev, int(record.get('rev') or 0))
        except (TypeError, ValueError):
            bad += 1
            continue
        op = record.get('op')
        if op == 'add':
            entry_id = record.get('id')
            if not isinstance(entry_id, str) or record.get('kind') not in KINDS:
                bad += 1
                continue
            if entry_id not in entries:
                order.append(entry_id)
            entries[entry_id] = dict(record, disabled=False, superseded=False,
                                     text=clean_text(record.get('text')))
            mid = record.get('message_id')
            if isinstance(mid, str):
                by_message[mid] = entry_id
        elif op == 'rename':
            entry_id = record.get('id')
            name = normalize_name(record.get('name'))
            if isinstance(entry_id, str) and entry_id in entries and name:
                entries[entry_id]['name'] = name
            else:
                bad += 1
        elif op == 'disable':
            entry_id = record.get('id')
            if isinstance(entry_id, str) and entry_id in entries:
                entries[entry_id]['disabled'] = True
        elif op == 'supersede':
            entry_id = record.get('id')
            if isinstance(entry_id, str) and entry_id in entries:
                entries[entry_id]['superseded'] = True
        else:
            bad += 1
    return {'entries': entries, 'order': order, 'by_message': by_message, 'rev': rev,
            'bad': bad, 'read_error': read_error}


def _views(replay, kinds):
    views = []
    for entry_id in replay['order']:
        entry = replay['entries'][entry_id]
        if entry['disabled'] or entry.get('superseded'):
            continue
        if entry['kind'] not in kinds:
            continue
        views.append({'id': entry['id'], 'kind': entry['kind'], 'expose': entry['expose'],
                      'name': entry.get('name') or auto_name(entry['text']),
                      'text': entry['text'], 'at_ms': entry.get('at_ms'),
                      'reply_to': entry.get('reply_to'), 'ref': entry.get('ref')})
    return views


def _error_for(replay):
    if replay['read_error']:
        return replay['read_error']
    if replay['bad']:
        return '有 %d 行读不出来（教材其余部分照常生效）' % replay['bad']
    return None


def state(home):
    """重放日志得到当前视图：两层（准则 / 资料）+ 版本号 + 显式的错误。"""
    replay = _replay(home)
    return {'rev': replay['rev'],
            'charter': _views(replay, CHARTER_KINDS),
            'notes': _views(replay, SEARCH_KINDS),
            'error': _error_for(replay),
            'bad_lines': replay['bad']}


def _write_line(path, record):
    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(handle, (json.dumps(record, ensure_ascii=False) + '\n').encode())
        os.fsync(handle)
    finally:
        os.close(handle)


def _next_id(replay):
    highest = 0
    for entry_id in replay['order']:
        try:
            highest = max(highest, int(entry_id.split('-')[-1]))
        except (TypeError, ValueError):
            continue
    return 't-%04d' % (highest + 1)


def _append_locked(home, record):
    replay = _replay(home)
    mid = record.get('message_id')
    if isinstance(mid, str) and mid in replay['by_message']:
        return dict(replay['entries'][replay['by_message'][mid]])
    if record.get('op') == 'rename':
        name = normalize_name(record.get('name'))
        if not name:
            raise ValueError('新名称不合规（要 2~%d 个字）' % MAX_NAME_CHARS)
        others = {view['name'] for view in _views(replay, CHARTER_KINDS) + _views(replay, NOTE_KINDS)
                  if view['id'] != record.get('id')}
        record = dict(record, name=_unique_name(name, others))
    if record.get('op') == 'add':
        text = ' '.join(str(record.get('text') or '').split())
        if not text:
            raise ValueError('教材正文为空')
        if len(text) > MAX_ITEM_CHARS:
            raise ValueError('条目超过 %d 字，缩到 %d 字以内再教' % (MAX_ITEM_CHARS, MAX_ITEM_CHARS))
        views = _views(replay, CHARTER_KINDS) + _views(replay, NOTE_KINDS)
        live = len(views)
        record = dict(record)
        record['name'] = _unique_name(record.get('name') or auto_name(text),
                                      {entry['name'] for entry in views})
        if live >= MAX_ENTRIES:
            raise ValueError('教材已满 %d 条，先说「撤回 t-0005」腾出位置再教' % MAX_ENTRIES)
    stored = dict(record, id=record.get('id') or _next_id(replay), rev=replay['rev'] + 1)
    _write_line(log_path(home), stored)
    return stored


def append(home, record):
    with _lock(home):
        return _append_locked(home, record)


def add_from_model(home, kind, text, message_id, at_ms=None, expose=None, name=None):
    """模型判出"这是教材"之后落盘——**写盘规矩仍在这一层**（模型只提供 kind 与正文）。

    幂等：同一条私聊消息（``message_id``）只落一次。消费者重试/重放时不会教出两条一模一样的教材；
    真需要改，本人去私聊里说「撤回 t-0005」再重说。
    """
    if not isinstance(message_id, str) or not message_id:
        raise ValueError('teach add 需要来源消息 id（幂等键）')
    with _lock(home):
        for entry in _replay(home)['entries'].values():
            if entry.get('message_id') == message_id and not entry.get('disabled'):
                return {'entry': entry, 'duplicate': True}
        record = new_entry(kind, text, message_id, int(at_ms or time.time() * 1000),
                           expose=expose, ref={'from': 'model'}, name=name)
        stored = _append_locked(home, record)
        return {'entry': stored, 'duplicate': False}


def disable(home, entry_id, at_ms=None):
    with _lock(home):
        return _append_locked(home, {'v': SCHEMA, 'op': 'disable', 'id': entry_id,
                                     'at_ms': int(at_ms if at_ms is not None else time.time() * 1000)})


def rename(home, key, name, at_ms=None):
    """改一条教材的名称（编号或旧名称都认）；没有这条/已失效返回 None。

    日志是只追加的：改名写一条 ``rename`` 记录，正文和其它字段一个字都不动，
    所以"改名字"永远不会顺手改掉内容。
    """
    entry_id = live_entry_id(home, key)
    if entry_id is None:
        return None
    with _lock(home):
        stored = _append_locked(home, {'v': SCHEMA, 'op': 'rename', 'id': entry_id,
                                       'name': name,
                                       'at_ms': int(at_ms if at_ms is not None else time.time() * 1000)})
    entry = find_entry(home, stored.get('id') or entry_id)
    return entry


def forget_last(home, at_ms=None):
    """撤回最后一条仍然生效的条目；没有可撤回的返回 None。"""
    with _lock(home):
        replay = _replay(home)
        live = [entry_id for entry_id in replay['order']
                if not replay['entries'][entry_id]['disabled']
                and not replay['entries'][entry_id].get('superseded')]
        if not live:
            return None
        entry = dict(replay['entries'][live[-1]])
        _append_locked(home, {'v': SCHEMA, 'op': 'disable', 'id': entry['id'],
                              'at_ms': int(at_ms if at_ms is not None else time.time() * 1000)})
        return entry


def entries(home):
    """全部条目的规范性视图（含已撤回的，带 status/confidence），检索与索引的唯一输入。

    为什么不让 memory 自己去读 jsonl：真身只有一个读者（本模块），否则"重放规则"会有两份，
    一份改了另一份不改，两边对同一条教材的判断就不一样了。
    """
    replay = _replay(home)
    result = []
    for entry_id in replay['order']:
        entry = replay['entries'][entry_id]
        if entry['disabled']:
            status = 'disabled'
        elif entry.get('superseded'):
            status = 'superseded'
        else:
            status = 'active'
        try:
            confidence = float(entry.get('confidence', 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        result.append({'id': entry['id'], 'kind': entry['kind'],
                       'expose': entry.get('expose'), 'text': entry['text'],
                       'status': status, 'confidence': max(0.0, min(1.0, confidence)),
                       'at_ms': entry.get('at_ms'), 'message_id': entry.get('message_id'),
                       'reply_to': entry.get('reply_to'), 'ref': entry.get('ref')})
    return result





_MENTION_PLACEHOLDER = re.compile(r'@_user_\d+')


def clean_text(text):
    """去掉飞书 @ 占位符（`@_user_1`）并收拢空白。

    占位符是消息结构，不是本人说的话。留着它会以 `<at>` 的残留形式进入每一次提示词
    （真机 2026-11-05 踩到：教材正文变成 "@_user_1 你可以用本机的 …"）。
    写的时候清一次，读的时候再清一次：旧条目不用重教也能干净显示。
    """
    if not isinstance(text, str):
        return ''
    return ' '.join(_MENTION_PLACEHOLDER.sub(' ', text).split())


# -- 问答回路 ---------------------------------------------------------------
#
# bridge 求助（notify）发出去了，本人引用回复那条私聊就是答复。这条链路要能回答三个问题：
# 这条回复答的是哪个问题（parent_id → dm_message_id）、答过了没有（幂等）、答完以后原来
# 那条消息谁去回（重放一个新事件；不能唤醒已经结束的进程）。

MAX_QUESTION_CHARS = 500


def _replay_questions(home):
    lines, read_error = _read_lines(questions_path(home))
    asks = {}
    answers = {}
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(record, dict):
            bad += 1
            continue
        qid = record.get('qid')
        if not isinstance(qid, str) or not qid:
            bad += 1
            continue
        if record.get('op') == 'ask':
            asks[qid] = record
        elif record.get('op') == 'answer':
            answers[qid] = record
        else:
            bad += 1
    return {'asks': asks, 'answers': answers, 'bad': bad, 'read_error': read_error,
            'pending': {qid: record for qid, record in asks.items() if qid not in answers}}


def register_question(home, event_id, question, needs, dm_message_id, chat_id=None, at_ms=None):
    """登记一个已发出的求助：同一个事件只登记一次（notify 重试不会产生两个待答）。"""
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        raise ValueError('question 需要一个整数事件 id')
    qid = 'q%d' % event_id
    with _lock(home):
        replay = _replay_questions(home)
        existing = replay['asks'].get(qid)
        if existing is not None:
            return dict(existing)
        record = {'v': SCHEMA, 'op': 'ask', 'qid': qid, 'event_id': event_id,
                  'question': ' '.join(str(question or '').split())[:MAX_QUESTION_CHARS],
                  'needs': needs if isinstance(needs, str) and needs else 'fact',
                  'dm_message_id': dm_message_id, 'chat_id': chat_id,
                  'at_ms': int(at_ms if at_ms is not None else time.time() * 1000)}
        _write_line(questions_path(home), record)
        return record


#: 待答求助的存活时间。为什么必须过期：2026-11-05 晚真机事故——两条求助（群里有人只发了个
#: 数字「14」「18」判不清意图）挂了一夜没人答，私聊从此对本人锁死：之后每条消息都被回
#: 「我有 2 个问题在等你，这条对不上号」，包括「帮我打开 UU APP」这种新指令，全被吞掉。
#: 求助本来就是"顺带问他要不要教我下次怎么办"，原消息早就按稳妥方式回过了；没人答就当没问过。
ASK_TTL_MS = 30 * 60 * 1000


def pending_questions(home, now_ms=None, ttl_ms=None):
    """还没答、且没过期的求助。过期的不再参与匹配（问句本身留在 questions.jsonl 里可查）。"""
    replay = _replay_questions(home)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    ttl = int(ttl_ms if ttl_ms is not None else ASK_TTL_MS)
    live = [record for record in replay['pending'].values()
            if now - int(record.get('at_ms') or 0) <= ttl]
    return sorted(live, key=lambda record: record.get('at_ms') or 0)


def question_for_parent(home, parent_id):
    """引用回复对上号：parent_id 必须等于某条求助私聊的 message_id。"""
    if not isinstance(parent_id, str) or not parent_id:
        return None
    for record in pending_questions(home):
        if record.get('dm_message_id') == parent_id:
            return record
    return None


def answer_question(home, qid, answer, answer_message_id, at_ms=None):
    """记下答复。返回 ``{'duplicate': True}`` 表示这个问题已经答过（重复回复不该再排一次）。"""
    with _lock(home):
        replay = _replay_questions(home)
        if qid in replay['answers']:
            return {'duplicate': True, 'qid': qid}
        question = replay['asks'].get(qid)
        if question is None:
            return {'duplicate': False, 'qid': qid, 'unknown': True}
        record = {'v': SCHEMA, 'op': 'answer', 'qid': qid,
                  'answer': ' '.join(str(answer or '').split())[:MAX_ITEM_CHARS],
                  'message_id': answer_message_id,
                  'at_ms': int(at_ms if at_ms is not None else time.time() * 1000)}
        _write_line(questions_path(home), record)
        return {'duplicate': False, 'qid': qid, 'question': question}


# -- 机械归并（⑤a）---------------------------------------------------------
#
# 只做两件不可能失真的事：删掉完全重复的、把同一天相邻同类的原文合成一条。
# 一个字都不改写：合并后的正文就是原来那几段原文按行拼接，所以「master 看到的内容」不变，
# 变的只是组织方式。模型归纳（⑤b）不在这一层，它必须带来源 id 并等本人确认。

def find_entry(home, key):
    """按编号或名称找一条教材，**包含已撤回/已被归并掉的**（历史可查，不是只看生效的）。

    名称是给本人用的（「汇报先给结论」），编号是机器用的（t-0007）：两种都能查，
    因为本人和模型各用各的说法，系统不该要求其中一方改用对方的语言。
    """
    replay = _replay(home)
    entry = replay['entries'].get(key)
    if entry is not None:
        return dict(entry)
    for entry_id in replay['order']:
        candidate = replay['entries'][entry_id]
        if (candidate.get('name') or auto_name(candidate.get('text'))) == key:
            return dict(candidate)
    return None


def live_entry_id(home, key):
    """编号或名称 → 仍然生效的条目编号；找不到/已失效返回 None（撤回只认生效的那条）。"""
    replay = _replay(home)
    if key in replay['entries']:
        entry = replay['entries'][key]
        if not entry['disabled'] and not entry.get('superseded'):
            return entry['id']
        return None
    for entry_id in reversed(replay['order']):
        entry = replay['entries'][entry_id]
        if entry['disabled'] or entry.get('superseded'):
            continue
        if (entry.get('name') or auto_name(entry.get('text'))) == key:
            return entry['id']
    return None


def _day_of(at_ms):
    try:
        return time.strftime('%Y-%m-%d', time.localtime(int(at_ms) / 1000))
    except (TypeError, ValueError, OSError):
        return ''


def tidy(home, dry_run=False):
    """删完全重复 + 合并同一天相邻同类；不改写任何文字，原条目保留可回退。

    返回 ``{'before','after','dropped_duplicates','merged','skipped','dry_run'}``。
    """
    with _lock(home):
        replay = _replay(home)
        live = [entry_id for entry_id in replay['order']
                if not replay['entries'][entry_id]['disabled']
                and not replay['entries'][entry_id].get('superseded')]
        seen = {}
        dropped = []
        keep = []
        for entry_id in live:
            entry = replay['entries'][entry_id]
            key = (entry['kind'], entry['text'])
            if key in seen:
                dropped.append(entry_id)
                continue
            seen[key] = entry_id
            keep.append(entry_id)
        groups = []
        current = []
        for entry_id in keep:
            entry = replay['entries'][entry_id]
            if current:
                previous = replay['entries'][current[-1]]
                if (previous['kind'] == entry['kind']
                        and _day_of(previous.get('at_ms')) == _day_of(entry.get('at_ms'))):
                    current.append(entry_id)
                    continue
                groups.append(current)
            current = [entry_id]
        if current:
            groups.append(current)
        candidates = [group for group in groups if len(group) > 1]
        plan = []
        for group in candidates:
            entries = [replay['entries'][entry_id] for entry_id in group]
            if any(entry.get('by') == 'tidy' for entry in entries):
                continue  # 已经归并出来的合成条目不再参与合并，免得反复套娃
            combined = '\n'.join(entry['text'] for entry in entries)
            if len(combined) > MAX_ITEM_CHARS:
                continue
            plan.append((group, combined))
        after = len(live) - len(dropped) - sum(len(group) for group, _ in plan) + len(plan)
        result = {'before': len(live), 'after': after, 'dropped_duplicates': len(dropped),
                  'merged': [group for group, _ in plan],
                  'skipped': len(candidates) - len(plan), 'dry_run': bool(dry_run)}
        if dry_run:
            return result
        now_ms = int(time.time() * 1000)
        for entry_id in dropped:
            _append_locked(home, {'v': SCHEMA, 'op': 'supersede', 'id': entry_id, 'at_ms': now_ms})
        for group, combined in plan:
            first = replay['entries'][group[0]]
            _append_locked(home, {'v': SCHEMA, 'op': 'add', 'id': _next_id(_replay(home)),
                                  'kind': first['kind'], 'expose': first['expose'],
                                  'text': combined, 'at_ms': now_ms,
                                  'message_id': 'tidy:%s' % '-'.join(group),
                                  'reply_to': None, 'ref': {'from': list(group)}, 'by': 'tidy'})
            for entry_id in group:
                _append_locked(home, {'v': SCHEMA, 'op': 'supersede', 'id': entry_id,
                                      'at_ms': now_ms})
        return result


def already_handled(home, message_id):
    """这条私聊消息以前处理过吗？处理过就不再回执。

    切到统一入口时，第一次扫描会按窗口回放（最长 1 个窗口）；旧通道已经回过执的消息
    必须在回放里被认出来，否则升级那一刻会往私聊里补发一串回执。
    """
    if not isinstance(message_id, str) or not message_id:
        return False
    for name in ('teachings.jsonl', 'questions.jsonl'):
        try:
            raw = (Path(home) / name).read_text()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('message_id') == message_id:
                return True
    return False
