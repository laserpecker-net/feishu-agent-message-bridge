"""`tools/state_guard.py`：验收前后状态护栏。

只测两件事：**基线里只放控制字段**（别让心跳之类的抖动造成假失败），以及
**mode/release 变了必须判失败**——2026-11-09 的事故就是"测试把 running 改回 held 却没人比对"。
"""

import json
from pathlib import Path

import pytest

from tools import state_guard


def test_parse_tolerates_banner_before_json():
    text = ('Warning: Permanently added host to the list of known hosts.\n'
            '{"home": "/home/<user>/.feishu-personal-agent", "mode": "held"}\n')
    assert state_guard.parse_version_json(text)['mode'] == 'held'


def test_parse_rejects_output_without_version_json():
    with pytest.raises(ValueError):
        state_guard.parse_version_json('feishu-bridge: error: invalid choice: version')


def test_snapshot_keeps_only_control_fields():
    taken = state_guard.snapshot({'mode': 'running', 'installed_release': 'a', 'running_release': 'a',
                                  'update_pending': False, 'heartbeat_age_seconds': 0.4,
                                  'installed_at': 1793950887, 'home': '/home/<user>'})
    assert taken == {'mode': 'running', 'installed_release': 'a',
                     'running_release': 'a', 'update_pending': False}


def test_mode_change_is_a_failure():
    changes, noise = state_guard.diff({'mode': 'running', 'installed_release': 'a',
                                       'running_release': 'a', 'update_pending': False},
                                      {'mode': 'held', 'installed_release': 'a',
                                       'running_release': 'a', 'update_pending': False})
    assert changes == [('mode', 'running', 'held')]


def test_release_change_is_a_failure():
    changes, _ = state_guard.diff({'mode': 'running', 'installed_release': 'a',
                                   'running_release': 'a', 'update_pending': False},
                                  {'mode': 'running', 'installed_release': 'b',
                                   'running_release': 'a', 'update_pending': True})
    assert ('installed_release', 'a', 'b') in changes
    assert ('update_pending', False, True) in changes


def test_heartbeat_churn_never_fails():
    changes, noise = state_guard.diff({'mode': 'running', 'installed_release': 'a',
                                       'running_release': 'a', 'update_pending': False,
                                       'heartbeat_age_seconds': 1.0, 'home': '/x'},
                                      {'mode': 'running', 'installed_release': 'a',
                                       'running_release': 'a', 'update_pending': False,
                                       'heartbeat_age_seconds': 9.0, 'home': '/x'})
    assert changes == []
    assert [item[0] for item in noise] == ['heartbeat_age_seconds']


def _run(monkeypatch, payload, argv):
    monkeypatch.setattr(state_guard, 'collect', lambda local, host, user: dict(payload))
    return state_guard.main(argv)


def test_save_then_compare_same_state_is_zero(tmp_path, monkeypatch):
    payload = {'mode': 'running', 'installed_release': 'a', 'running_release': 'a',
               'update_pending': False, 'heartbeat_age_seconds': 0.5}
    baseline = tmp_path / 'pre.json'
    assert _run(monkeypatch, payload, ['--local', '--save', str(baseline)]) == 0
    assert json.loads(baseline.read_text())['mode'] == 'running'
    assert _run(monkeypatch, payload, ['--local', '--compare', str(baseline)]) == 0


def test_compare_after_state_drift_exits_three(tmp_path, monkeypatch):
    baseline = tmp_path / 'pre.json'
    baseline.write_text(json.dumps({'mode': 'running', 'installed_release': 'a',
                                    'running_release': 'a', 'update_pending': False}))
    drifted = {'mode': 'held', 'installed_release': 'a', 'running_release': 'a',
               'update_pending': False}
    assert _run(monkeypatch, drifted, ['--local', '--compare', str(baseline)]) == 3


def test_release_only_drift_does_not_tell_you_to_hold_or_release(tmp_path, monkeypatch, capsys):
    """升级本来就会改 release：这时提示"跑 hold/release"是错的，会把人引去乱动挂起状态。"""
    baseline = tmp_path / 'pre.json'
    baseline.write_text(json.dumps({'mode': 'running', 'installed_release': 'old',
                                    'running_release': 'old', 'update_pending': False}))
    upgraded = {'mode': 'running', 'installed_release': 'new', 'running_release': 'new',
                'update_pending': False}
    assert _run(monkeypatch, upgraded, ['--local', '--compare', str(baseline)]) == 3
    printed = capsys.readouterr().out
    assert 'feishu-bridge hold' not in printed and 'feishu-bridge release' not in printed
    assert '只有 release 变了' in printed


def test_mode_drift_does_tell_you_which_switch_restores_it(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / 'pre.json'
    baseline.write_text(json.dumps({'mode': 'running', 'installed_release': 'a',
                                    'running_release': 'a', 'update_pending': False}))
    drifted = {'mode': 'held', 'installed_release': 'a', 'running_release': 'a',
               'update_pending': False}
    assert _run(monkeypatch, drifted, ['--local', '--compare', str(baseline)]) == 3
    assert 'feishu-bridge release' in capsys.readouterr().out
