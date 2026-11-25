"""待人工项与关单：``feishu-bridge attention list`` / ``attention close`` 的实现。

为什么需要这个出口：一条消息失败会生成一条待办通知（``attention`` 事件），过去它们
**只能手改库**——没有关单路径就没有闭环，账本上会永远挂着已经不需要处理的东西。

两条不可越过的语义：

1. **关单必须留理由**（``--note``）：以后看账本要能分清「人看过了，不值得再管」
   和「它自己成功发出去了」。
2. **关单不伪造回复**：只改状态，不写 outbox、不改 ``reply_id``。它不是补一条回复，
   是「这件事到此为止」。
"""
import time

INBOX_PREFIX = 'inbox:'
OUTBOX_PREFIX = 'outbox:'
EVENT_PREFIX = 'event:'


def parse_ref(ref):
    """把 ``inbox:om_…`` / ``outbox:265`` / ``event:351`` 解成 ``(类别, id)``；格式不对返回 None。"""
    if not isinstance(ref, str):
        return None
    if ref.startswith(INBOX_PREFIX):
        message_id = ref[len(INBOX_PREFIX):].strip()
        return ('inbox', message_id) if message_id else None
    if ref.startswith(OUTBOX_PREFIX):
        raw = ref[len(OUTBOX_PREFIX):].strip()
        if not raw.isdigit():
            return None
        return ('outbox', int(raw))
    if ref.startswith(EVENT_PREFIX):
        raw = ref[len(EVENT_PREFIX):].strip()
        if not raw.isdigit():
            return None
        return ('event', int(raw))
    return None


def _preview(text, limit=40):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def items(store):
    """把三类待人工项拉平成一张表用的行（顺序固定：先消息、再出站、最后事件）。"""
    data = store.attention_items()
    rows = []
    for row in data['inbox']:
        rows.append({'ref': INBOX_PREFIX + row['id'], 'kind': '消息', 'state': '待人工',
                     'when': row['received_at'], 'reason': row['error'] or '',
                     'preview': _preview(row.get('text'))})
    for row in data.get('outbox') or []:
        rows.append({'ref': OUTBOX_PREFIX + str(row['row_id']), 'kind': '出站', 'state': '待人工',
                     'when': row['created_at'], 'reason': row['error'] or '',
                     'preview': _preview(row.get('body'))})
    for row in data['events']:
        rows.append({'ref': EVENT_PREFIX + str(row['id']), 'kind': '待办', 'state': row['state'],
                     'when': row['created_at'], 'reason': row['error'] or '',
                     'preview': _preview(_source_of(row))})
    return rows


def _source_of(row):
    """待办事件的来源消息 id（payload 里），人看着能对上号。"""
    import json
    try:
        payload = json.loads(row.get('payload') or '{}')
    except ValueError:
        return ''
    return payload.get('message_id') or ''


def format_table(rows):
    """给人看的表：可粘贴的 ref 在最左，末尾两行汇总。"""
    lines = ['%-28s  %-4s  %-6s  %-19s  %s' % ('ref（可粘贴）', '类别', '状态', '时间', '原因 / 来源')]
    for row in rows:
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['when'] or 0))
        detail = row['reason'] or ''
        if row['kind'] == '待办' and row['preview']:
            detail = (detail + ' ← ' + row['preview']).strip()
        elif row['kind'] == '消息' and row['preview']:
            detail = (detail + ' | ' + row['preview']).strip(' |')
        lines.append('%-28s  %-4s  %-6s  %-19s  %s' % (row['ref'], row['kind'], row['state'],
                                                       when, detail))
    inbox = sum(1 for row in rows if row['kind'] == '消息')
    outbox = sum(1 for row in rows if row['kind'] == '出站')
    events = len(rows) - inbox - outbox
    lines.append('待处理 inbox %d 条 / outbox %d 条 / 事件 %d 条' % (inbox, outbox, events))
    return '\n'.join(lines)


def close(store, ref, note, now=None):
    """按 ref 关单；返回 ``{ok, ref, before, after, error}``（不抛异常，交给 CLI 定退出码）。"""
    now = time.time() if now is None else now
    parsed = parse_ref(ref)
    if parsed is None:
        return {'ok': False, 'ref': ref,
                'error': 'ref 格式不对：要写 inbox:<消息 id> / outbox:<行号> / event:<事件 id>'}
    kind, ident = parsed
    if kind == 'inbox':
        return store.dismiss_inbox(ident, note, now)
    if kind == 'outbox':
        return store.close_outbox(ident, note, now)
    return store.close_attention_event(ident, note, now)


def format_close(result):
    """关单前后对比，一行一条。"""
    if not result.get('ok'):
        return '没关：%s' % result.get('error')
    line = '%s：%s → %s' % (result.get('ref'), result.get('before'), result.get('after'))
    if result.get('events_closed'):
        line += '（同时关掉配套待办 %d 条）' % result['events_closed']
    return line + '；理由：' + str(result.get('error_after'))
