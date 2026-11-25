"""`feishu-bridge consumer status` 的判定逻辑。

只测一件事：**它回答的是"现在谁在领事件"**，而不是"某个已经退役的引擎跑没跑"。
权威数据只有 `state.sqlite3` 的 `consumers` 表（每个消费者每轮 register 都刷新 last_seen_at），
已退役引擎写的 `consumer-status.json` 不再参与任何判定。
"""

import json
from pathlib import Path
import tempfile
import time

import pytest

from bridge import consumerctl
from bridge.store import Store


def home_with(directory, **consumers):
    home = Path(directory)
    (home / 'installation.json').write_text(json.dumps({'release_id': 'rel1'}))
    store = Store(home / 'state.sqlite3')
    for name, age in consumers.items():
        store.register_consumer(name.replace('_', '-'), 'other', time.time() - age)
    return home


def test_live_consumer_is_reported_healthy():
    with tempfile.TemporaryDirectory() as directory:
        home = home_with(directory, agentkit_cursor=3)
        data = consumerctl.status(home)
        assert data['healthy'] is True
        assert [item['consumer'] for item in data['live_consumers']] == ['agentkit-cursor']
        assert 'agentkit-cursor' in data['note']
        assert data['installed_release'] == 'rel1'


def test_stale_consumer_is_not_live():
    with tempfile.TemporaryDirectory() as directory:
        home = home_with(directory, agentkit_cursor=consumerctl.LIVE_CONSUMER_SECONDS + 60)
        data = consumerctl.status(home)
        assert data['healthy'] is False
        assert data['live_consumers'] == []
        assert '没有活跃消费者' in data['note']


def test_stale_legacy_status_file_never_decides_anything():
    """回归：旧引擎写的 consumer-status.json 里的 healthy/pid 全是陈旧的，
    以前它能让 status 误报"挂了"或"正常"。现在它必须被完全忽略。"""
    with tempfile.TemporaryDirectory() as directory:
        home = home_with(directory, agentkit_cursor=2)
        (home / 'consumer-status.json').write_text(json.dumps({
            'consumer': 'cursor-main', 'pid': 42, 'heartbeat_at': time.time(),
            'last_poll_at': time.time(), 'running_release': 'rel1'}))
        data = consumerctl.status(home)
        assert data['healthy'] is True
        assert data['consumers'][0]['consumer'] == 'agentkit-cursor'
        assert not any(item['consumer'] == 'cursor-main' for item in data['consumers'])


def test_missing_database_degrades_to_a_note_instead_of_raising():
    with tempfile.TemporaryDirectory() as directory:
        data = consumerctl.status(Path(directory))
        assert data['consumers'] == [] and data['healthy'] is False
        assert 'agentkit-consumerctl.py' in data['note']


def test_live_consumers_absent_when_database_missing():
    with tempfile.TemporaryDirectory() as directory:
        assert consumerctl.live_consumers(Path(directory)) == []


def test_only_status_is_accepted_as_a_subcommand(capsys):
    """生命周期不在这里：`consumer start/stop/restart` 已经随引擎 A 一起删除。"""
    consumerctl.main(Path('/nonexistent'), ['status'])
    assert 'consumers' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        consumerctl.main(Path('/nonexistent'), ['start'])


def test_held_with_stale_consumer_is_reported_as_expected_idle():
    """挂起期间消费者按语义不领事件、也不再 register —— 这时 healthy=false 是**事实**，
    但"可能已停止"是误报。status 必须把 hold 这条事实说出来，并给出 idle_expected。"""
    from bridge import hold as hold_switch
    with tempfile.TemporaryDirectory() as directory:
        home = home_with(directory, dsh_main=consumerctl.LIVE_CONSUMER_SECONDS + 600)
        hold_switch.write_state(home, {'active': True, 'since_ms': int(time.time() * 1000) - 3600_000,
                                       'by': 'ops', 'reason': '有意挂起'})
        data = consumerctl.status(home)
        assert data['healthy'] is False          # 没人在领事件，这是事实
        assert data['idle_expected'] is True     # 但这是挂起造成的，不是故障
        assert data['hold']['active'] is True
        assert data['hold']['by'] == 'ops'
        assert '挂起' in data['note']
        assert '可能已停止' not in data['note']


def test_hold_cleared_goes_back_to_the_plain_stale_note():
    from bridge import hold as hold_switch
    with tempfile.TemporaryDirectory() as directory:
        home = home_with(directory, dsh_main=consumerctl.LIVE_CONSUMER_SECONDS + 600)
        hold_switch.write_state(home, {'active': False})
        data = consumerctl.status(home)
        assert data['idle_expected'] is False
        assert data['hold']['active'] is False
        assert '可能已停止' in data['note']


def test_live_consumer_under_hold_is_still_reported_live():
    """挂起刚生效、消费者还没到下一轮 register 时，它仍然是活的：别说成挂起空闲。"""
    from bridge import hold as hold_switch
    with tempfile.TemporaryDirectory() as directory:
        home = home_with(directory, dsh_main=2)
        hold_switch.write_state(home, {'active': True, 'since_ms': int(time.time() * 1000),
                                       'by': 'ops'})
        data = consumerctl.status(home)
        assert data['healthy'] is True
        assert data['idle_expected'] is False
        # 挂起期间"刚登记过"不等于"在领事件"：note 不能写成「当前在领事件的是 …」。
        assert '挂起' in data['note']
        assert '不领事件' in data['note']
        assert '当前在领事件的是' not in data['note']
