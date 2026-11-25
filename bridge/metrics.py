"""一条消息的耗时分解：采集 / 消费者 / 投递。

为什么要有这个模块：笼统地说"它回得慢"没法验收——改轮询间隔的人和改模型的人会互相看不清
对方的效果。分成三段之后每一段都有自己的负责人：

- ``collect``  = 消息在飞书里发出 → bridge 采集到（归采集间隔）
- ``consumer`` = 采集到 → 答复已生成（归模型单轮 + 消费者管道）
- ``deliver``  = 生成 → 飞书读回确认（归 sender）

口径（这三条决定了数字可不可信，改之前先改这里）：

1. 分位数用 **nearest-rank、不插值**：样本少的时候插值会造出一个谁都没经历过的数。
2. ``verified_at`` 为空用 ``sent_at`` 兜底；两者都空就**不算这条的投递耗时**，只计入
   ``deliver_missing``——算成 0 秒会让投递看起来完美，正好把问题藏起来。
3. 负区间（时钟回拨、采集时间早于消息时间）**不进分位数**，只计入 ``negatives``；
   混进去会把 p50 拉低，让人误以为变快了。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_WINDOW = 50
#: 没有答复的消息也算"在队列里"，单独报数（沉默必须有迹象）
PENDING_STATES = ('external_queued', 'details_pending', 'reply_queued', 'needs_attention')


def _percentile(values, fraction):
    """nearest-rank：第 ⌈f·n⌉ 个（1 起），不插值。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(-(-len(ordered) * fraction // 1)))  # ceil(f*n)
    return ordered[min(rank, len(ordered)) - 1]


def _round(value):
    return None if value is None else round(value, 1)


def _stage(values):
    return {'samples': len(values), 'p50': _round(_percentile(values, 0.5)),
            'p90': _round(_percentile(values, 0.9)),
            'min': _round(min(values)) if values else None,
            'max': _round(max(values)) if values else None}


def _connect(home):
    path = Path(home) / 'state.sqlite3'
    if not path.exists():
        raise FileNotFoundError(str(path))
    return sqlite3.connect('file:%s?mode=ro' % path, uri=True)


def _scan_interval(home):
    try:
        config = json.loads((Path(home) / 'config.json').read_text())
    except (OSError, ValueError):
        return None
    value = config.get('live_interval_seconds')
    return value if isinstance(value, int) and value > 0 else None


def _empty(window):
    """还没采到任何消息的 home：给同样的字段、全零，而不是抛异常或编一个数出来。"""
    stage = _stage([])
    return {'window': window,
            'stages': {'collect': dict(stage), 'consumer': dict(stage), 'deliver': dict(stage)},
            'total': dict(stage),
            'scan_interval_seconds': None,
            'samples': {'answered': 0, 'pending': 0, 'deliver_missing': 0, 'negatives': 0}}


def latency(home, window=DEFAULT_WINDOW):
    """最近 ``window`` 条已答消息的耗时分解（只读，不改任何东西）。"""
    window = max(1, int(window))
    collect, consume, deliver, totals = [], [], [], []
    negatives = deliver_missing = answered = 0
    try:
        connection = _connect(home)
    except FileNotFoundError:
        return _empty(window)
    with connection as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT i.create_ms, i.received_at, o.created_at, o.sent_at,
                                    o.first_send_at, o.verified_at
                             FROM inbox i
                             JOIN outbox o ON o.inbox_id = i.id AND o.kind = 'reply'
                             WHERE i.received_at IS NOT NULL
                             ORDER BY i.received_at DESC LIMIT ?''', (window,)).fetchall()
        pending = db.execute('''SELECT COUNT(*) FROM inbox WHERE state IN (%s)'''
                             % ','.join('?' * len(PENDING_STATES)),
                             PENDING_STATES).fetchone()[0]
    for row in rows:
        created = (row['create_ms'] or 0) / 1000.0
        received = row['received_at'] or 0
        generated = row['created_at'] or 0
        # 投递完成时刻：优先"飞书读回确认"，其次"发送成功"；都没有就不算投递样本
        finished = row['verified_at'] or row['sent_at']
        spans = [received - created if created else None,
                 generated - received if received and generated else None,
                 (finished - generated) if finished and generated else None]
        if any(value is not None and value < 0 for value in spans):
            negatives += 1
            continue
        answered += 1
        if spans[0] is not None:
            collect.append(spans[0])
        if spans[1] is not None:
            consume.append(spans[1])
        if spans[2] is None:
            deliver_missing += 1
        else:
            deliver.append(spans[2])
        if all(value is not None for value in spans):
            totals.append(sum(spans))
    return {'window': window,
            'stages': {'collect': _stage(collect), 'consumer': _stage(consume),
                       'deliver': _stage(deliver)},
            'total': _stage(totals),
            'scan_interval_seconds': _scan_interval(home),
            'samples': {'answered': answered, 'pending': pending,
                        'deliver_missing': deliver_missing, 'negatives': negatives}}
