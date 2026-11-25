"""`tools/bridge-remote.sh check` 的退出码语义：verify.sh 失败必须让 check 失败。

2026-11-05 真机：同事机 verify.sh 输出 `VERIFY RESULT=failed`（hold.json 键读错），
而 `bridge-remote.sh check` 的退出码仍是 0 —— 远端是 `verify.sh | tail`，管道退出码取的是 tail 的。
于是文档里「check 退出码 0 才算成功」变成了假绿：验收会通过，机器其实是坏的。
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = ROOT / 'tools' / 'bridge-remote.sh'


def _fake_ssh(tmp_path, verify_rc):
    """假 ssh：读到含 verify.sh 的脚本就按 verify_rc 退出，其余当成 doctor 成功。"""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    script = bindir / 'ssh'
    script.write_text(
        '#!/usr/bin/env bash\n'
        'stdin="$(mktemp)"\n'
        'cat > "$stdin"\n'
        'if grep -q "verify.sh" "$stdin"; then\n'
        '  echo "VERIFY release_match=ok"\n'
        '  echo "VERIFY RESULT=%s"\n' % ('failed' if verify_rc else 'ok') +
        '  rc=%d\n' % verify_rc +
        'else\n'
        '  echo \'{"verdict":"ok"}\'\n'
        '  rc=0\n'
        'fi\n'
        'rm -f "$stdin"\n'
        'exit $rc\n')
    script.chmod(0o755)
    return bindir


def _check(bindir):
    return subprocess.run(
        ['bash', str(REMOTE), '--host', 'fake-host.tailnet', '--user', 'tester', 'check'],
        capture_output=True, text=True,
        env={'PATH': '%s:/usr/bin:/bin' % bindir, 'HOME': str(bindir.parent)})


def test_check_fails_when_verify_fails(tmp_path):
    result = _check(_fake_ssh(tmp_path, 3))
    assert 'VERIFY RESULT=failed' in result.stdout, result.stdout
    assert result.returncode == 3, result.stdout + result.stderr


def test_check_passes_when_everything_is_green(tmp_path):
    result = _check(_fake_ssh(tmp_path, 0))
    assert 'VERIFY RESULT=ok' in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_propagates_the_verify_exit_code_not_the_pipeline_tail():
    text = REMOTE.read_text()
    block = text[text.index('    verify_code=0'):text.index('  ps-tests)')]
    # 远端必须在自己这边捕获 verify.sh 的退出码，再原样退出（管道只负责截取输出）
    assert 'out="$(bash dist/wsl2/wsl/verify.sh 2>&1)"; vrc=$?' in block
    assert 'exit "$vrc"' in block
    assert 'exit $verify_code' in text[:text.index('  ps-tests)')]
