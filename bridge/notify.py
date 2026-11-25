"""本人私聊通知的去重与限额规则（纯函数，方便测试）。

为什么需要它：``consumer_runtime`` 会在两种情况下给本人发私聊——Agent 主动求助
（``ask_owner``）和自己答不上来（超时/失败）时的漏答提醒。没有闸门时，一次突发（今天
一位同事的机器 2 分钟内进了 15 条 @）会变成十几条私聊，比不通知更烦；而重试和崩溃恢复
又会让同一条通知重复发。所以规则是：

* **同键去重**：同一个 ``dedupe_key`` 在 ``DEDUPE_WINDOW_SECONDS`` 内只发一次
  （重试、恢复、同一个事件的重复提交都落到同一个键上）；
* **每小时上限**：滑动小时内最多 ``MAX_PER_HOUR`` 条，超了就不发（宁可漏提醒，不可刷屏）；
* **状态**：只有发送成功才记账；发送失败不动状态，下一次还能再试。

状态文件是 ``home/notify.json``（本机运行期产物，不进仓库）。它坏掉/读不出来时按"空状态"
处理并重新开始记账——通知是可再生的，不值得为它阻塞任何主流程。
"""

import json
import time

DEDUPE_WINDOW_SECONDS = 6 * 3600
MAX_PER_HOUR = 6
STATE_NAME = 'notify.json'


def state_path(home):
    return home / STATE_NAME


def read_state(home):
    """读状态；缺失/坏 JSON/非对象一律当空状态（通知不值得为状态文件阻塞）。"""
    try:
        value = json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _sent(value):
    sent = value.get('sent')
    return sent if isinstance(sent, dict) else {}


def _recent(value, now, window):
    """返回窗口内仍有效的 (key, ts) 列表，顺带丢掉过期项。"""
    rows = []
    for key, ts in _sent(value).items():
        if isinstance(key, str) and isinstance(ts, (int, float)) and 0 <= now - ts < window:
            rows.append((key, ts))
    return rows


def decide(value, key, now=None, window=DEDUPE_WINDOW_SECONDS, cap=MAX_PER_HOUR):
    """判断这次通知该不该发，返回 ``('send'|'duplicate'|'capped', 说明)``。

    ``duplicate``：同一个键在窗口内已经发过（重试/恢复的常态，不是错误）。
    ``capped``：滑动小时内的通知已达上限——这说明"正在发生一次突发"，此时应该保持安静。
    """
    now = time.time() if now is None else now
    recent = _recent(value, now, window)
    keys = {item for item, _ in recent}
    if isinstance(key, str) and key in keys:
        return 'duplicate', '{} 秒内已就同一件事通知过本人'.format(int(window))
    if len([ts for _, ts in recent if now - ts < 3600]) >= cap:
        return 'capped', '最近一小时已通知 {} 次，达到上限；这次不再打扰本人'.format(cap)
    return 'send', ''


def record(value, key, now=None, chat_id=None):
    """把一次成功发送记进状态，返回新的状态对象（窗口外的旧键顺手清掉）。"""
    now = time.time() if now is None else now
    state = dict(value) if isinstance(value, dict) else {}
    sent = {item: ts for item, ts in _sent(state).items()
            if isinstance(ts, (int, float)) and 0 <= now - ts < DEDUPE_WINDOW_SECONDS}
    if isinstance(key, str) and key:
        sent[key] = now
    state['sent'] = sent
    state['last_at'] = now
    if isinstance(chat_id, str) and chat_id:
        # 记下 dm 会话 id：以后要做"本人私聊回复→事件"的回路时，这就是轮询用的 chat_id。
        state['owner_dm_chat_id'] = chat_id
    return state


def body_key(body):
    """默认去重键：正文哈希。同一段话重复发，一律合并。"""
    import hashlib
    return hashlib.sha256((' '.join(str(body or '').split())).encode('utf-8')).hexdigest()[:32]
