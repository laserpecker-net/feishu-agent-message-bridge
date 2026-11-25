"""安装器第 8 步验收的判据必须可复跑。

2026-11-20 真机暴露：安装器"看起来成功"与用户"真的能用"是两件事——回话端 enable 了但没 start，
干活端单元压根没生成。所以 setup.sh 的验收加了两条：两个消费者都 live、干活端 --check 通过。

这里把那段 Python 验收块抽出来，用合成输入跑三种情形：缺消费者 / 自检 FAIL / 全过。
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = (ROOT / 'dist' / 'wsl2' / 'setup.sh').read_text()


def acceptance_source():
    start = SETUP.index('python3 - "$STATUS_TMP"')
    body = SETUP[start:]
    body = body[body.index("<<'PY'") + len("<<'PY'"):]
    return body[:body.index('\nPY\n')]


def run(tmp_path, *, consumers, worker_check):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    (home / 'status.json').write_text(json.dumps({'identity': {'openId': 'ou_test'}}), encoding='utf-8')
    (home / 'config.json').write_text(json.dumps({'target_open_id': 'ou_test'}), encoding='utf-8')
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({'process_running': True, 'authenticated': True,
                                  'workers': {'collector': True, 'details': True, 'sender': True}}),
                      encoding='utf-8')
    cons = tmp_path / 'consumers.json'
    cons.write_text(json.dumps({'consumers': consumers}), encoding='utf-8')
    check = tmp_path / 'worker_check.txt'
    check.write_text(worker_check, encoding='utf-8')
    script = tmp_path / 'accept.py'
    script.write_text(acceptance_source(), encoding='utf-8')
    return subprocess.run(
        [sys.executable, str(script), str(status), str(home), 'ou_test', str(ROOT), str(cons), str(check)],
        capture_output=True, text=True)


def test_missing_worker_consumer_fails_with_actionable_next_step(tmp_path):
    done = run(tmp_path, consumers=[{'consumer': 'dsh-main', 'live': True}],
               worker_check='干活端可用：consumer=dsh-worker\n')
    assert done.returncode == 1
    assert 'dsh-worker 没登记' in done.stdout
    assert 'install-dsh-worker.sh' in done.stdout


def test_registered_but_not_live_fails(tmp_path):
    done = run(tmp_path, consumers=[{'consumer': 'dsh-main', 'live': True},
                                    {'consumer': 'dsh-worker', 'live': False}],
               worker_check='干活端可用：consumer=dsh-worker\n')
    assert done.returncode == 1
    assert 'live != true' in done.stdout
    assert 'journalctl' in done.stdout


def test_worker_self_check_failure_is_a_failure_even_with_both_live(tmp_path):
    # 有 'FAIL' 行就必须失败：曾经这里只查了"能不能打印那行标题"，等于给 FAIL 放行。
    done = run(tmp_path, consumers=[{'consumer': 'dsh-main', 'live': True},
                                    {'consumer': 'dsh-worker', 'live': True}],
               worker_check='干活端可用：consumer=dsh-worker\n  cli_version 未知 FAIL\n')
    assert done.returncode == 1
    assert '干活端自检没过' in done.stdout
    assert 'cli_version 未知 FAIL' in done.stdout


def test_all_green_passes(tmp_path):
    done = run(tmp_path, consumers=[{'consumer': 'dsh-main', 'live': True},
                                    {'consumer': 'dsh-worker', 'live': True}],
               worker_check='干活端可用：consumer=dsh-worker\n  launch  cmd-interop  PASS\n')
    assert done.returncode == 0, done.stdout + done.stderr
    assert '两个消费者都 live' in done.stdout
