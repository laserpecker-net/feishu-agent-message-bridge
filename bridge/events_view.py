"""事件视图：CLI 与 HTTP 网关共用的唯一实现。

同一个事件行只能有一种 JSON 形状。这两份视图原本各写了一遍（字段不同、`claim_token`
出现条件也不同），改一处就会漏另一处——第二个消费者拿到的 payload 和第一个不一样。
"""

import json


def parse_payload(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def event_view(event, detailed=False, with_token=False, teachings=None):
    """事件行的对外视图。

    ``teachings`` 不为 None 时注入到 payload 里（只在 claim 路径上传，消费进程按事件粒度
    启停，所以教材必须随每条事件重新注入）。payload 不是对象时抛 ValueError：宁可显式失败，
    也不静默交出一个没有教材的 payload——那样模型会以为自己被教过。
    """
    payload = parse_payload(event['payload'])
    if teachings is not None:
        if not isinstance(payload, dict):
            raise ValueError('event %s payload is not an object; refusing to inject teachings'
                             % event['id'])
        payload['teachings'] = teachings
    view = {'id': event['id'], 'kind': event['kind'], 'dedupe_key': event['dedupe_key'],
            'payload': payload, 'created_at': event['created_at'],
            'attempts': event['attempts'], 'lease_until': event['lease_until']}
    if detailed:
        view.update(state=event['state'], consumer=event['consumer'],
                    next_due=event['next_due'], error=event['error'])
    if with_token:
        view['claim_token'] = event.get('claim_token')
    return view
