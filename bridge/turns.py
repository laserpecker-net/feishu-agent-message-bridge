"""归因日志（L2-b）：每次回复记一条——这一轮到底发生了什么、用了哪几条教材、结果如何。

为什么单独一个文件而不进 SQLite：和教材同一套理由（见 ``bridge/teach.py`` 顶部）——事件库有
``PRAGMA user_version`` 门禁，把"每轮都写的高频日志"塞进去会把回滚变成数据迁移。这里是一份
追加日志（``home/turns.jsonl``，0600，flock），按 ``event_id`` 幂等，重放取最后一条。

一条 turn 记什么（只记**发生时**确定的事实，不记事后推测）：

- ``message_id`` / ``chat_id`` / ``event_id`` / ``consumer``：这轮是哪条消息、谁做的；
- ``outcome``：``sent``（回复已提交）/ ``skipped``（模型判断不用回，一条消息都没发）/
  ``failed``（适配器失败或死信）/ ``timeout``（租约到期）；
- ``error``：失败原因（服务端给的原文，不加工）；
- ``used_ids``：**claim 那一刻**注入给模型的教材 id（由 bridge 在 claim 时落到事件行上，
  所以回复回来时还是"模型真正看到的那几条"，不会因为中途教材变动而对不上）；
- ``task_id``：这轮回填了哪本任务账本。

``reflection()`` 只做**机械归纳**：按失败的类别计数、按证据挑一条候选教训，文本是模板拼的、
每句都能指到具体的 turn —— 不做"模型自己反思自己"（那需要另一次生成，且无法验证真伪）。
"""

import fcntl
import json
import os
import time
from pathlib import Path

SCHEMA = 1
OUTCOMES = ('sent', 'skipped', 'failed', 'timeout')
#: 复盘看最近多少轮（再多就不是"最近"了）。
REFLECT_WINDOW = 50
#: 同一类失败至少出现几次才值得变成一条教训（一次算意外，两次才是模式）。
MIN_FAILURES_FOR_LESSON = 2
#: 日志上限：超过就保留最近这么多条（在写入时顺手截断，见 ``record``）。
MAX_TURNS = 5000


def log_path(home):
    return Path(home) / 'turns.jsonl'


def _lock(home):
    path = Path(home) / 'turns.lock'
    handle = open(path, 'a')
    os.chmod(path, 0o600)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _read(path):
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return [], None
    except OSError as error:
        return [], '%s: %s' % (type(error).__name__, error)
    records, bad = [], 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            bad += 1
    return records, ('有 %d 行读不出来' % bad if bad else None)


def record(home, *, message_id, event_id, outcome, chat_id=None, consumer=None, error=None,
           used_ids=None, task_id=None, at_ms=None):
    """追加一轮。同一 ``event_id`` 再来一次只保留最新的一条（重试/重启都幂等）。"""
    if outcome not in OUTCOMES:
        raise ValueError('未知 outcome %r（只认 %s）' % (outcome, '/'.join(OUTCOMES)))
    entry = {'v': SCHEMA, 'at_ms': int(at_ms if at_ms is not None else time.time() * 1000),
             'message_id': message_id, 'event_id': event_id, 'chat_id': chat_id,
             'consumer': consumer, 'outcome': outcome,
             'error': (str(error)[:200] if error else None),
             'used_ids': [str(item) for item in (used_ids or [])][:40],
             'task_id': task_id}
    path = log_path(home)
    handle = _lock(home)
    try:
        existing, _ = _read(path)
        kept = [item for item in existing if item.get('event_id') != event_id]
        kept.append(entry)
        if len(kept) > MAX_TURNS:
            kept = kept[-MAX_TURNS:]
        temporary = path.with_suffix('.jsonl.tmp')
        with temporary.open('w') as out:
            for item in kept:
                out.write(json.dumps(item, ensure_ascii=False) + '\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return entry


def turns(home, limit=50, outcome=None):
    """最近的 turn（新的在前）。``outcome`` 可以过滤成只看见成功/失败/超时。"""
    records, error = _read(log_path(home))
    items = [item for item in records if outcome is None or item.get('outcome') == outcome]
    items.sort(key=lambda item: int(item.get('at_ms') or 0))
    result = list(reversed(items))
    if not isinstance(limit, int) or limit <= 0:
        limit = 50
    return {'count': len(result), 'error': error, 'turns': result[:limit]}


def _normalize_error(value):
    """把服务端错误归成类，避免每个 message id 都算一类（复盘要看模式，不是看编号）。"""
    text = str(value or '').strip()
    if not text:
        return '未知原因'
    head = text.split(':', 1)[0].strip()
    for known in ('AgentFailed', 'InsufficientTime', 'Timeout', 'TimeoutExpired',
                  'dead', 'attention'):
        if head.lower() == known.lower():
            return known
    return head[:40]


def summary(home, window=REFLECT_WINDOW):
    """最近若干轮的样子：成功/失败比、失败的类别、教材用了几条、有没有被截断。"""
    recent = turns(home, limit=window)['turns']
    by_outcome = {name: 0 for name in OUTCOMES}
    errors = {}
    injected = 0
    used_total = 0
    unattributed = 0
    failures = []
    for item in recent:
        outcome = item.get('outcome')
        if outcome in by_outcome:
            by_outcome[outcome] += 1
        ids = item.get('used_ids') or []
        if ids:
            injected += 1
            used_total += len(ids)
        elif outcome == 'sent':
            unattributed += 1
        if outcome in ('failed', 'timeout'):
            errors[_normalize_error(item.get('error'))] = \
                errors.get(_normalize_error(item.get('error')), 0) + 1
            failures.append({'event_id': item.get('event_id'),
                             'message_id': item.get('message_id'),
                             'outcome': outcome, 'error': item.get('error'),
                             'at_ms': item.get('at_ms')})
    return {'window': window, 'turns': len(recent), 'by_outcome': by_outcome,
            'errors': sorted(errors.items(), key=lambda item: -item[1]),
            'teachings': {'turns_with_teachings': injected,
                          'average_used': (round(used_total / injected, 1) if injected else 0),
                          'sent_without_attribution': unattributed},
            'failures': failures[:10]}


#: 失败类别 → 候选教训。每一条都是**具体做法**，不是"要更努力"这种空话。
LESSON_BY_ERROR = {
    'AgentFailed': '这类活一次做不完：先拆成小步，每步做完就回一句话说进度，别憋到最后一起说。',
    'InsufficientTime': '动手之前先回一句"收到，正在做"，别等全部做完才出声，以免超时后一句话都没留下。',
    'Timeout': '先给一个最短的可用答复，再补细节；不要为了完整而把回复拖过时间窗。',
    'TimeoutExpired': '先给一个最短的可用答复，再补细节；不要为了完整而把回复拖过时间窗。',
    'dead': '连续失败时不要反复重试同一条路：说清卡在哪、缺什么，让本人决定怎么继续。',
}


def reflection(home, window=REFLECT_WINDOW):
    """复盘 = 一段可读的报告 + 最多一条候选教训（要本人回「记」才落盘）。

    返回 ``{'report': str, 'candidate': dict|None, 'summary': dict}``。挑候选的规则是机械的：
    失败次数最多的那一类，且出现次数 ≥ ``MIN_FAILURES_FOR_LESSON``；没有模式就不硬编一条。
    """
    stats = summary(home, window=window)
    lines = ['复盘（最近 %d 轮）' % stats['turns']]
    if not stats['turns']:
        lines.append('· 还没有记录：先让我答几条消息，这里才有东西可复盘。')
        return {'report': '\n'.join(lines), 'candidate': None, 'summary': stats}
    by = stats['by_outcome']
    lines.append('· 答完 %d 次、判为不用回 %d 次、失败 %d 次、超时 %d 次'
                 % (by['sent'], by['skipped'], by['failed'], by['timeout']))
    if stats['errors']:
        lines.append('· 失败的原因：' + '，'.join('%s×%d' % (name, count)
                                             for name, count in stats['errors']))
    teach = stats['teachings']
    if teach['turns_with_teachings']:
        lines.append('· 教材：平均每轮用 %s 条；有 %d 轮回复没带上教材使用记录'
                     % (teach['average_used'], teach['sent_without_attribution'])
                     if teach['sent_without_attribution'] else
                     '· 教材：平均每轮用 %s 条' % teach['average_used'])
    for failure in stats['failures'][:3]:
        lines.append('· 最近一次没答完：%s（%s）' % (failure.get('message_id'),
                                                 failure.get('error') or failure.get('outcome')))
    candidate = None
    if stats['errors']:
        name, count = stats['errors'][0]
        if count >= MIN_FAILURES_FOR_LESSON:
            text = LESSON_BY_ERROR.get(name)
            if text:
                # 证据按时间正序列出（人读起来像"发生过的顺序"，不是倒序）
                evidence = list(reversed([item['event_id'] for item in stats['failures']
                                          if _normalize_error(item.get('error')) == name]))[:5]
                candidate = {'kind': 'lesson', 'text': '教训：' + text,
                             'evidence': evidence, 'error_kind': name, 'count': count}
    if candidate:
        lines.append('')
        lines.append('要不要把这条记成教材（回「记」落盘，回「不记」丢掉）：')
        lines.append(candidate['text'])
    else:
        lines.append('')
        lines.append('最近没有反复出现的失败模式，这次不编教训。')
    return {'report': '\n'.join(lines), 'candidate': candidate, 'summary': stats}
