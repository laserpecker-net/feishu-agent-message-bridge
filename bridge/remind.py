"""提醒：时间的读法、写法，以及"到点该发什么"的那一句话（Python 3.9 stdlib）。

只做纯函数 + 一处时间解析，不发消息也不碰库：谁发、什么时候发、发失败怎么办，
分别归 ``bridge/service.py``（心跳里抢一条发一条）和 ``bridge/store.py``（reminders 表）。
这样 CLI、服务、消费契约三处对"21:40 是哪一刻"只有一种读法。
"""

import re
import time

#: 本人/模型能写出来的两种时间写法：``2026-11-06 21:40[:05]`` 与 ISO 的 ``T`` 版本。
#: 只认本地时区（这台机器与本人同一个时区），**不猜 UTC**——猜错了提醒就在半夜响。
_DATE_TIME = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$')
_HOUR_MINUTE = re.compile(r'^(\d{1,2}):(\d{2})$')

#: 迟到多久才在提醒正文里标注。几十秒的抖动不值得说，十几分钟必须说，
#: 否则本人会以为"它记错了时间"。
LATE_NOTE_SECONDS = 90

MAX_TEXT_CHARS = 200
MAX_NAME_CHARS = 16


def format_when(ms):
    """毫秒 → ``2026-11-06 21:40``（本地时区，分钟精度：提醒不需要秒）。"""
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(int(ms) / 1000))
    except (TypeError, ValueError, OSError):
        return ''


def to_ms(value, now_ms=None):
    """把 ``2026-11-06 21:40`` / ``21:40`` / 毫秒数解析成本地时区毫秒；解析不了抛 ValueError。

    ``21:40`` 这种只有时刻的写法按"最近一次还没到的 21:40"算：今天还没到就是今天，
    已经过了就是明天——本人说"晚上九点四十提醒我"时不会想听"那个点已经过了"。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value or '').strip()
    if not text:
        raise ValueError('时间不能为空（写成 2026-11-06 21:40 或 21:40）')
    if text.isdigit():
        # 只有像毫秒时间戳的数才收（大约 2001~2200 年）；`20261106214000` 这种"把日期连写"的
        # 数字若是照单全收，会变成一个几千年后的提醒，而本人以为设的是明天。
        stamp = int(text)
        if not 10 ** 12 <= stamp < 7 * 10 ** 12:
            raise ValueError('时间读不出来：%s（要毫秒时间戳就写 13 位，例如 1793790000000）' % text)
        return stamp
    match = _DATE_TIME.match(text)
    if match:
        parts = [int(part) if part is not None else 0 for part in match.groups()]
        year, month, day, hour, minute, second = parts
        if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 59):
            raise ValueError('时间读不出来：%s（月 1~12、日 1~31、时 0~23、分/秒 0~59）' % text)
        stamp = (year, month, day, hour, minute, second, 0, 1, -1)
        try:
            ms = int(time.mktime(stamp) * 1000)
        except (OverflowError, ValueError) as error:
            raise ValueError('时间读不出来：%s（%s）' % (text, error))
        # mktime 会把 2 月 30 日这种日子悄悄滚到 3 月：回读对不上就是本人写错了日期，
        # 说出来比替他改成另一天安全（提醒响在错的日子等于没提醒）。
        back = time.localtime(ms / 1000)[:6]
        if tuple(back) != (year, month, day, hour, minute, second):
            raise ValueError('没有这一天：%s' % text)
        return ms
    match = _HOUR_MINUTE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError('时间读不出来：%s（小时 0~23，分钟 0~59）' % text)
        today = time.localtime(now_ms / 1000)
        day = today.tm_mday + (1 if _is_past(now_ms, hour, minute) else 0)
        # 交给 mktime 进位（跨月/跨年都对），不自己加 86400000：有夏令时的时区会差一小时。
        return int(time.mktime((today.tm_year, today.tm_mon, day, hour, minute,
                                0, 0, 1, -1)) * 1000)
    raise ValueError('时间读不出来：%s（写成 2026-11-06 21:40 或 21:40）' % text)


def _is_past(now_ms, hour, minute):
    local = time.localtime(now_ms / 1000)
    return (local.tm_hour, local.tm_min) >= (hour, minute)


def describe(ms, now_ms=None):
    """人话的相对时间：``9 分钟后`` / ``1 小时后`` / ``已经过了 3 分钟``。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    delta = int(ms) - now_ms
    if delta <= -60000:
        return '已经过了 %d 分钟' % max(1, int(-delta // 60000))
    if delta < 60000:
        return '马上'
    minutes = int(delta // 60000)
    if minutes < 60:
        return '%d 分钟后' % minutes
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return '%d 小时后' % hours
    if hours < 24:
        return '%d 小时 %d 分钟后' % (hours, rest)
    days, hours = divmod(hours, 24)
    return '%d 天 %d 小时后' % (days, hours) if hours else '%d 天后' % days


def body_for(row, now_ms=None):
    """到点要发出去的那一句：谁说的、什么时候说的、原定几点，迟到就明说。

    刻意**不带 @**：提醒是给本人的主动消息，@ 会把 bridge 自己的消息变成下一条待办
    （扫描只收 ``is_at_me`` 的消息，等于自己给自己派活）。``tests/test_remind.py`` 钉住。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    text = str(row.get('text') or '').strip() or '（没写内容）'
    asked = format_when(row.get('created_at_ms') or now_ms)
    lines = ['⏰ 提醒你：%s' % text]
    note = '（你 %s 让我这个点提醒你）' % asked
    fire_at = row.get('fire_at_ms')
    if fire_at is not None and now_ms - int(fire_at) > LATE_NOTE_SECONDS * 1000:
        # 迟到要说，但不能把"你几点让我提醒的"换掉：两件事都得说，否则像系统自己编的一条。
        note = '（你 %s 让我这个点提醒你；原定 %s，晚了 %d 分钟）' % (
            asked, format_when(fire_at), max(1, int((now_ms - int(fire_at)) // 60000)))
    lines.append(note)
    return '\n'.join(lines)
