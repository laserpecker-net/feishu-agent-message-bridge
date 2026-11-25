"""只读的消费者清单：现在谁在领事件、多活跃、用的哪个身份。

事件消费者的**生命周期**不在这里：macOS 归 LaunchAgent
`io.feishu-agent.agentkit-consumer`（切换与回滚用 `agentkit-consumerctl.py`），
Windows/WSL2 归 systemd unit（`io.feishu-agent.dsh-consumer`）。
这里只回答"谁在干活"，权威数据是 `state.sqlite3` 的 `consumers` 表——
每个消费者每轮都会 `events register` 刷新 `last_seen_at`。
"""
import argparse
import json
from pathlib import Path
import time

from . import hold as hold_switch
from .store import Store

#: 「最近还活着」的门槛：正常轮询间隔（5s）远小于它；超过就只当历史身份。
LIVE_CONSUMER_SECONDS = 120


def live_consumers(home, now=None, within=LIVE_CONSUMER_SECONDS):
    """从权威表读出现存消费者身份与实际活跃度。

    这里刻意不读 `consumer-status.json`：那个文件是已退役的 Cursor 引擎
    （旧 `bridge/consumer.py`）写的，留着它只会把"旧引擎停了"误报成"没人回复"。
    """
    database = Path(home) / 'state.sqlite3'
    if not database.exists():
        return []
    now = time.time() if now is None else now
    try:
        rows = Store(database).consumers()
    except Exception:  # 数据库被锁/损坏时不要因为一个清单字段而 raise
        return []
    observed = []
    for row in rows:
        age = now - (row.get('last_seen_at') or 0)
        observed.append({'consumer': row.get('name'), 'kind': row.get('kind'),
                         'last_seen_age_seconds': round(age, 1),
                         'claims': row.get('claims', 0), 'acks': row.get('acks', 0),
                         'fails': row.get('fails', 0), 'live': age < within})
    return sorted(observed, key=lambda item: item['last_seen_age_seconds'])


def status(home):
    home = Path(home)
    observed = live_consumers(home)
    live = [item for item in observed if item['live']]
    installed = None
    try:
        installed = json.loads((home / 'installation.json').read_text()).get('release_id')
    except (OSError, ValueError):
        pass
    # 挂起是**事实**（hold.json，`bridge.hold.read_state` 是唯一读取口），不是推断：
    # 挂起期间消费者按语义不领事件、也不再每轮 register，`last_seen_at` 自然变旧。
    # 不把这条事实说出来，`healthy=false` + "可能已停止" 就成了误报——消费者进程好着，
    # 只是被挂起门禁挡住了（2026-11-06 在挂起的同事机上读到的原话）。
    hold = hold_switch.read_state(home)
    hold_active = bool(hold.get('active'))
    data = {'consumers': observed, 'live_consumers': live, 'healthy': bool(live),
            'installed_release': installed,
            'hold': {'active': hold_active,
                     'since': hold_switch.clock(hold.get('since_ms')) if hold_active else None,
                     'by': hold.get('by'), 'reason': hold.get('reason')},
            'idle_expected': hold_active and not live}
    if not observed:
        data['note'] = ('没有任何消费者登记过：macOS 用 agentkit-consumerctl.py start 起一个，'
                        'WSL2 用 systemd unit')
    elif hold_active:
        # 挂起期间"刚登记过"也不等于"在领事件"：进程起来时 register 一次，之后被门禁挡住。
        # 两种状态都说成"挂起中"，区别只在最近一次登记的新鲜度，别让读者以为它在干活。
        freshest = observed[0]
        if live:
            data['note'] = ('服务处于挂起（held，自 %s，by %s）：消费者进程在跑'
                            '（最近登记 %ss 前），按挂起语义不领事件；release 之后自动恢复领取'
                            % (data['hold']['since'], data['hold']['by'] or '未知',
                               freshest['last_seen_age_seconds']))
        else:
            data['note'] = ('服务处于挂起（held，自 %s，by %s）：按挂起语义消费者不领事件，这是预期；'
                            '最近一次登记是 %ss 前，release 之后一个轮询周期内会重新登记'
                            % (data['hold']['since'], data['hold']['by'] or '未知',
                               freshest['last_seen_age_seconds']))
    elif live:
        data['note'] = '当前在领事件的是 ' + '、'.join(item['consumer'] for item in live)
    else:
        freshest = observed[0]
        data['note'] = ('当前没有活跃消费者；最近的 %s 是 %ss 前（可能已停止）'
                        % (freshest['consumer'], freshest['last_seen_age_seconds']))
    return data


#: 删除消费者的保护窗口：最近 10 分钟还登记过的，默认拒绝（它可能正在领事件）。
FORGET_GUARD_SECONDS = 600


def forget(home, name, force=False, now=None):
    """删掉一行消费者身份。死行清理的正规出口，不手改库。

    保护规则：``last_seen_at`` 在最近 10 分钟内的消费者默认拒绝删除——那说明它还在跑，
    删了等于把它下一轮 ``register`` 前的状态清空（回执会说"消费者未注册"）。
    真要在它活着时删（比如换名字），``--force`` 明确表示"我知道它会重新登记"。
    """
    home = Path(home)
    database = home / 'state.sqlite3'
    if not database.exists():
        raise LookupError('未找到状态库 %s' % database)
    now = time.time() if now is None else now
    store = Store(str(database))
    try:
        row = next((item for item in store.consumers() if item.get('name') == name), None)
        if row is None:
            raise LookupError('没有这个消费者：%s' % name)
        age = now - float(row.get('last_seen_at') or 0)
        if age < FORGET_GUARD_SECONDS and not force:
            raise PermissionError('%s 最近 %.0f 秒前还在登记（<10 分钟），可能正在领事件；'
                                  '确认要删就加 --force' % (name, age))
        if not store.forget_consumer(name):
            raise LookupError('没有这个消费者：%s' % name)
        return {'ok': True, 'forgotten': {'name': name, 'kind': row.get('kind'),
                                         'last_seen_age_seconds': round(age, 1),
                                         'claims': row.get('claims', 0), 'acks': row.get('acks', 0),
                                         'fails': row.get('fails', 0)}}
    finally:
        store.close()


def main(home, argv):
    parser = argparse.ArgumentParser(prog='feishu-bridge consumer',
                                     description='消费者身份：看谁在领事件（status）、清理死行（forget）')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('status', help='现有消费者与活跃度（默认）')
    item = commands.add_parser('forget', help='删掉一行消费者身份（死行清理的正规出口）')
    item.add_argument('name', help='消费者名（consumers.name）')
    item.add_argument('--force', action='store_true', help='最近 10 分钟内还登记过也删（它可能还在跑）')
    args = parser.parse_args(argv or ['status'])
    command = args.command or 'status'
    if command == 'status':
        print(json.dumps(status(home), ensure_ascii=False, indent=2))
        return 0
    try:
        print(json.dumps(forget(home, args.name, force=args.force), ensure_ascii=False, indent=2))
    except LookupError as error:
        raise SystemExit(str(error))
    except PermissionError as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 1
    return 0
