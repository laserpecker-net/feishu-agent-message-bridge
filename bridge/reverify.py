"""把「发出去却被误判」的终回复改回来：``feishu-bridge outbox reverify`` 的实现。

为什么需要这个命令：2026-11-18 发现回读校验把「含 @ 的回复」逐字比较，238 条不含 @ 的
全过、14 条含 @ 的一次没过——行被标成 ``needs_attention`` 并附「no resend」，而飞书上
早已发出去。判据修好之后，历史行不能手改库：要么重判通过、要么原样留着，两者都要能复跑。
"""
import json
import time

from .transport import verify_reply_reason


def reverify(store, transport, target, now=None, dry_run=False, limit=None):
    """逐行重判，返回 ``[{row_id, inbox_id, reply_id, ok, reason}]``。

    只在**重判确实通过**时迁移状态（``Store.reverify_outbox`` 还会再挡一道：只认那两类
    可修复的错误）。``dry_run`` 只报告不写，重跑已修好的行不会被再次计数。
    """
    now = time.time() if now is None else now
    results = []
    for row in store.outbox_attention_replies(limit):
        job = store.get_message(row['inbox_id']) if row.get('inbox_id') else None
        reason = ''
        if row.get('reply_id') is None:
            reason = '没有回读的回复 id，无法重判'
        elif job is None:
            reason = '来源消息已不在收件箱'
        else:
            original = json.loads(job['raw_json'])
            reply = transport.get_message(row['reply_id'])
            reason = verify_reply_reason(reply, original, row['body'], target)
        ok = not reason
        if ok and not dry_run and not store.reverify_outbox(row['row_id'], now):
            ok, reason = False, '状态已变（不再是被误判的终回复）'
        results.append({'row_id': row['row_id'], 'inbox_id': row.get('inbox_id'),
                        'reply_id': row.get('reply_id'), 'ok': ok,
                        'reason': reason or '通过'})
    return results


def format_table(results):
    """给人看的表：``row_id / 结果 / 原因``，末尾一行汇总。"""
    lines = ['%8s  %-6s  %s' % ('row_id', '结果', '原因')]
    for item in results:
        lines.append('%8d  %-6s  %s' % (item['row_id'], '修复' if item['ok'] else '仍失败',
                                        item['reason']))
    fixed = sum(1 for item in results if item['ok'])
    lines.append('修复 %d 行 / 仍失败 %d 行' % (fixed, len(results) - fixed))
    return '\n'.join(lines)


def close_stale(store, now=None, dry_run=False):
    """关掉「来源已处理完、待办还挂着」的僵尸事件；``dry_run`` 只数不改。"""
    now = time.time() if now is None else now
    return store.close_stale_attention_events(now, dry_run=dry_run)


def format_stale(result, dry_run=False):
    """一行汇总：``--close-stale`` 关掉多少、还剩多少。"""
    if dry_run:
        return '僵尸待办（来源已完成）：将关掉 %d 条 / 现在还剩 %d 条' % (
            result['closed'], result['remaining'])
    return '僵尸待办（来源已完成）：已关掉 %d 条 / 还剩 %d 条' % (
        result['closed'], result['remaining'])
