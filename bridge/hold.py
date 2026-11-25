"""Bridge 挂起开关：hold（下班）与 release（上班）。

语义（见 docs/bridge-hold-switch-requirements.md §13-15，均已与需求方确认）：

* 挂起期间 **零采集、零入队、零回复**：collector 不执行任何搜索扫描，inbox / outbox /
  events 不会新增行；进程与心跳继续存在，因此状态可观测。
* 挂起是**持久**的：重启、断电、`systemctl restart` 之后依然挂起，不会自动恢复。
* 恢复时把 ``service_epoch_ms`` 前移到恢复时刻，让"关闭窗口"在时间轴上被切掉：
  之后任何回溯扫描返回的该窗口消息都会命中 ``before_service_activation`` 而被忽略。
* 控制通道只能**轮询自聊会话**（个人账号身份收不到推送事件），并且只认本人发出的
  精确指令；其余消息读完即丢，不落库。

状态文件 ``home/hold.json``：原子写（临时文件 + replace），服务端按 mtime+size 缓存读取。
文件缺失或损坏时 **fail-open**：按"未挂起"处理并在日志里告警。
"""

import json
import re
import time
from pathlib import Path

HOLD_FILE = 'hold.json'
SCHEMA = 1

_MENTION = re.compile(r'@_user_\d+')
_PUNCTUATION = ' \t\r\n\u00a0\u3000:：,，.。!！?？;；、()（）[]【】"\'“”‘’'


def hold_path(home):
    return Path(home) / HOLD_FILE


def default_state():
    return {'schema': SCHEMA, 'active': False, 'since_ms': None, 'by': None, 'reason': None,
            'last_release_at_ms': None, 'released_by': None, 'dropped_at_release': 0,
            'corrupt': False}


def read_state(home):
    """读取挂起状态；缺失/损坏一律按未挂起返回（fail-open），并用 corrupt 标记告警。"""
    state = default_state()
    try:
        raw = hold_path(home).read_text()
    except FileNotFoundError:
        return state
    except OSError:
        state['corrupt'] = True
        return state
    try:
        value = json.loads(raw)
    except ValueError:
        state['corrupt'] = True
        return state
    if not isinstance(value, dict):
        state['corrupt'] = True
        return state
    merged = dict(state)
    merged.update({key: value[key] for key in state if key in value})
    merged['active'] = bool(merged.get('active'))
    for key in ('since_ms', 'last_release_at_ms', 'dropped_at_release'):
        merged[key] = _as_int(merged.get(key))
    merged['dropped_at_release'] = merged['dropped_at_release'] or 0
    merged['corrupt'] = False
    return merged


def _as_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def write_state(home, state):
    """原子写入；CLI 与服务都可能写，最后一次 replace 生效，不存在半写文件。"""
    payload = {key: value for key, value in state.items() if key != 'corrupt'}
    payload['schema'] = SCHEMA
    temp = hold_path(home).with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(hold_path(home))
    return payload


def is_active(home):
    return bool(read_state(home).get('active'))


def normalize(text):
    """去掉 @ 占位符、首尾空白与标点，统一小写后比较。"""
    value = _MENTION.sub('', text or '')
    return value.strip(_PUNCTUATION).lower()


def clock(ms):
    """把毫秒时间戳格式化成当地时间 HH:MM（用于回执文案）。"""
    if not ms:
        return '未知时间'
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ms / 1000.0))


def hold_notice(state):
    since = clock(state.get('since_ms'))
    reason = state.get('reason')
    lines = ['已下班（挂起中）',
             '· 此期间的 @ 消息不会进入队列，也不会回复',
             '· 发送「上班」或执行 feishu-bridge release 恢复',
             '· 开始时间：' + since]
    if reason:
        lines.append('· 原因：' + str(reason)[:80])
    return '\n'.join(lines)


def release_notice(dropped=0, since=None):
    """恢复回执：终端与飞书自聊必须**逐字一致**（同一条命令、同一个函数）。

    ``dropped is None`` 表示服务没在跑、条数还算不出来——这时明确说"未确认"，
    不要编一个"无积压消息"（那会把丢消息说成没丢）。
    """
    lines = ['已恢复接待']
    if dropped is None:
        lines.append('· 挂起期间的积压已作废；具体条数待服务启动后见 status 的 hold.dropped_during_hold')
    elif dropped:
        lines.append('· 挂起期间丢弃 %d 条消息（不会补答）' % dropped)
    elif since:
        lines.append('· 挂起期间无积压消息')
    lines.append('· 从现在起的新消息正常处理')
    return '\n'.join(lines)
