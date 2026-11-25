"""WSL2 消费者单元的静态断言：它必须跑 release 里的副本，而不是仓库树。

这是一条**跨平台一致性**的护栏：macOS 的消费者一直在 `releases/<id>/` 里，
只要 Windows 侧有人把 `ExecStart` 改回仓库树，"升级后跑的是哪份代码"就又变成两套说法。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = (ROOT / 'dist' / 'wsl2' / 'wsl' / 'install-dsh-consumer.sh').read_text()
FINALIZE = (ROOT / 'dist' / 'wsl2' / 'wsl' / 'finalize-systemd.sh').read_text()


def test_unit_execstart_points_at_the_release_copy():
    assert 'ExecStart=/usr/bin/python3 $CONSUMER_ENTRY' in INSTALLER
    assert 'CONSUMER_ENTRY="$HOME_BRIDGE/current/dsh-consumer.py"' in INSTALLER
    # 仓库树不再是运行入口（只能作为装包的来源）
    assert '$REPO/dsh-consumer.py' not in INSTALLER


def test_installer_refuses_when_release_has_no_consumer_runtime():
    assert '[ -f "$CONSUMER_ENTRY" ] || fail' in INSTALLER


def test_restart_flag_restarts_the_unit_for_a_new_release():
    assert '--restart) RESTART=1' in INSTALLER
    assert 'systemctl restart "$UNIT_NAME"' in INSTALLER


def test_check_reports_which_release_the_unit_runs():
    assert 'emit current_release' in INSTALLER
    assert 'emit unit_execstart' in INSTALLER


def test_finalize_delegates_consumer_reload_to_the_single_implementation():
    # 重载逻辑只能有一份（reload-consumer.sh）：安装路径与升级路径都调它。
    # 以前 finalize 内联 `sudo bash install-dsh-consumer.sh --restart`——非交互（无 tty）时
    # sudo 直接失败、失败被吞成 warning，于是消费者一直跑旧 release（2026-11-03 真机事故）。
    assert 'CONSUMER_UNIT="io.feishu-agent.dsh-consumer.service"' in FINALIZE
    assert 'bash "$HERE/reload-consumer.sh"' in FINALIZE
    # 安装器只允许出现在"遗留单元名 → 点号新名"的一次性迁移分支里（2026-11-04 审计 F1）；
    # 日常重载路径必须仍然只有 reload-consumer.sh 一份实现。
    migration = FINALIZE[FINALIZE.index('CONSUMER_LEGACY_UNITS='):FINALIZE.index('say \'重载 DSH 消费者')]
    assert 'install-dsh-consumer.sh' in migration
    assert 'install-dsh-consumer.sh' not in FINALIZE.replace(migration, '')
    # 迁移必须发生在 bridge 起来之后、且遗留名单必须在清理旧 unit 之前抓取。
    assert migration.index('restart "$UNIT"') < migration.index('install-dsh-consumer.sh')
    assert FINALIZE.index('CONSUMER_LEGACY_UNITS=') < FINALIZE.index('bash "$HERE/migrate-legacy-units.sh"')


def test_finalize_sudoers_covers_the_consumer_unit():
    # 重载消费者需要 root；免密规则必须覆盖那个单元，否则脚本里的 sudo -n 必然失败。
    assert 'restart $CONSUMER_UNIT' in FINALIZE
    assert 'is-active $CONSUMER_UNIT' in FINALIZE


def test_reload_consumer_script_is_the_one_implementation():
    reload_script = ROOT / 'dist' / 'wsl2' / 'wsl' / 'reload-consumer.sh'
    assert reload_script.exists()
    text = reload_script.read_text()
    # 没装消费者 = 合法跳过（不能凭空装一个）
    assert '跳过重载' in text
    # 非交互：没有免密就立刻失败，不能卡在密码提示上
    assert 'sudo -n' in text
    # 只重启不重写 unit（unit 由 install-dsh-consumer.sh 负责）
    assert 'restart "$UNIT"' in text
    assert 'install-dsh-consumer.sh' not in text
    # 必须自证生效：启动时间没变就是没重启，不许报成功
    assert 'ActiveEnterTimestamp' in text
    assert '启动时间没变' in text


def test_consumer_reload_has_exactly_one_call_site():
    # 重载消费者只发生在 D 托管（升级路径也会跑 D：Environment→Code→Bridge→Finalize）。
    # 升级脚本自己再重启一遍 = 两份"什么时候该重载"的判断，各自演化只是时间问题。
    upgrade = (ROOT / 'dist' / 'wsl2' / 'wsl' / 'upgrade.sh').read_text()
    assert 'reload-consumer.sh' not in upgrade
    ps1 = (ROOT / 'dist' / 'wsl2' / 'install-windows.ps1').read_text()
    assert ps1.count('Install-PhaseFinalize') >= 1
    # C 阶段（含"已装走升级"分支）不许自己再跑一遍托管
    c_phase = ps1[ps1.index('function Install-PhaseBridge'):ps1.index('function Install-PhaseFinalize')]
    assert 'finalize-systemd.sh' not in c_phase


def test_verify_fails_when_a_consumer_runs_under_another_unit_name(tmp_path):
    """没有 canonical 单元时不能无条件当绿。

    2026-11-04 真机形态：`com.<人名>.feishu-dsh-consumer.service` 一直在跑（consumers 表
    last_seen 是新鲜的），而 verify 只找 canonical → `consumer_release=not-installed` → 通过。
    """
    import sqlite3
    import subprocess
    import time

    home = tmp_path / 'home'
    (home / '.feishu-personal-agent').mkdir(parents=True)
    (home / '.feishu-personal-agent' / 'installation.json').write_text(
        '{"installed_at": %d}' % int(time.time()))
    (home / '.feishu-personal-agent' / 'config.json').write_text('{"target_open_id": "ou_x"}')
    db = sqlite3.connect(str(home / '.feishu-personal-agent' / 'state.sqlite3'))
    db.execute('CREATE TABLE consumers (name TEXT, kind TEXT, created_at REAL, last_seen_at REAL)')
    db.execute('INSERT INTO consumers VALUES (?,?,?,?)', ('dsh-main', 'dsh', time.time(), time.time()))
    db.commit()
    db.close()

    unit_dir = tmp_path / 'units'
    unit_dir.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in (
            ('systemctl', '#!/usr/bin/env bash\nif [ "$1" = show ]; then echo ""; exit 0; fi\nexit 0\n'),
            ('getent', '#!/usr/bin/env bash\necho "tester:x:1000:1000::%s:/bin/bash"\n' % home),
            ('lark-cli', '#!/usr/bin/env bash\nexit 1\n')):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)

    result = subprocess.run(
        ['bash', str(ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh'), '--user', 'tester'],
        capture_output=True, text=True,
        env={'PATH': '%s:/usr/bin:/bin' % bindir, 'SYSTEMD_UNIT_DIR': str(unit_dir),
             'HOME': str(home)})
    assert 'consumer_release=unmanaged' in result.stdout, result.stdout
    assert result.returncode == 3


def test_verify_asserts_the_consumer_runs_the_current_release():
    # 光"重载"还不够：验收必须能发现"bridge 升级了但消费者还在跑旧代码"。
    verify = (ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh').read_text()
    assert 'consumer_release' in verify
    assert 'ActiveEnterTimestamp' in verify
    assert 'installed_at' in verify
    # 旧 → 判 fail（不是 warn），否则 check 会给出 ok 的假象
    assert 'bad consumer_release' in verify


def test_verify_flags_a_consumer_that_still_runs_an_old_release(tmp_path):
    """真跑一遍 verify.sh：消费者单元比当前 release 旧 → consumer_release 必须判 fail。

    只做静态断言不够——这条检查的全部价值在于"它会不会真的响"。这里用假 systemd
    （SYSTEMD_UNIT_DIR + PATH 里的假 systemctl）在 macOS 上也能跑。
    """
    import subprocess

    home = tmp_path / 'home'
    (home / '.feishu-personal-agent').mkdir(parents=True)
    import time
    consumer_start = time.mktime(time.strptime('Tue 2023-11-14 22:13:20', '%a %Y-%m-%d %H:%M:%S'))
    (home / '.feishu-personal-agent' / 'installation.json').write_text(
        '{"installed_at": %d}' % (int(consumer_start) + 100))
    (home / '.feishu-personal-agent' / 'config.json').write_text('{"target_open_id": "ou_x"}')

    unit_dir = tmp_path / 'units'
    unit_dir.mkdir()
    (unit_dir / 'io.feishu-agent.dsh-consumer.service').write_text('[Unit]\n')

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    # 消费者启动时间（1700000000）早于 release 落地时间（1700000100）→ 旧
    (bindir / 'systemctl').write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = show ]; then echo "Tue 2023-11-14 22:13:20 UTC"; exit 0; fi\n'
        'if [ "$1" = is-active ]; then echo active; exit 0; fi\n'
        'exit 0\n')
    (bindir / 'systemctl').chmod(0o755)
    (bindir / 'getent').write_text(
        '#!/usr/bin/env bash\necho "tester:x:1000:1000::%s:/bin/bash"\n' % home)
    (bindir / 'getent').chmod(0o755)
    (bindir / 'lark-cli').write_text('#!/usr/bin/env bash\nexit 1\n')
    (bindir / 'lark-cli').chmod(0o755)

    result = subprocess.run(
        ['bash', str(ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh'), '--user', 'tester'],
        capture_output=True, text=True,
        env={'PATH': '%s:/usr/bin:/bin' % bindir, 'SYSTEMD_UNIT_DIR': str(unit_dir),
             'HOME': str(home)})
    assert 'VERIFY consumer_release=stale' in result.stdout, result.stdout
    assert result.returncode == 3


def test_verify_accepts_a_consumer_that_was_restarted_after_the_release(tmp_path):
    import subprocess

    home = tmp_path / 'home'
    (home / '.feishu-personal-agent').mkdir(parents=True)
    import time
    installed_at = time.mktime(time.strptime('Tue 2023-11-14 22:13:20', '%a %Y-%m-%d %H:%M:%S'))
    (home / '.feishu-personal-agent' / 'installation.json').write_text(
        '{"installed_at": %d}' % int(installed_at))
    (home / '.feishu-personal-agent' / 'config.json').write_text('{"target_open_id": "ou_x"}')

    unit_dir = tmp_path / 'units'
    unit_dir.mkdir()
    (unit_dir / 'io.feishu-agent.dsh-consumer.service').write_text('[Unit]\n')

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'systemctl').write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = show ]; then echo "Tue 2023-11-14 22:40:00 UTC"; exit 0; fi\n'
        'if [ "$1" = is-active ]; then echo active; exit 0; fi\n'
        'exit 0\n')
    (bindir / 'systemctl').chmod(0o755)
    (bindir / 'getent').write_text(
        '#!/usr/bin/env bash\necho "tester:x:1000:1000::%s:/bin/bash"\n' % home)
    (bindir / 'getent').chmod(0o755)
    (bindir / 'lark-cli').write_text('#!/usr/bin/env bash\nexit 1\n')
    (bindir / 'lark-cli').chmod(0o755)

    result = subprocess.run(
        ['bash', str(ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh'), '--user', 'tester'],
        capture_output=True, text=True,
        env={'PATH': '%s:/usr/bin:/bin' % bindir, 'SYSTEMD_UNIT_DIR': str(unit_dir),
             'HOME': str(home)})
    assert 'VERIFY consumer_release=ok' in result.stdout, result.stdout


def test_consumer_unit_name_is_one_fact_across_every_script():
    """单元名只能有一个答案：安装器创建的名字必须与其余脚本引用的一致。

    2026-11-04 的升级事故就出在这里：安装器创建 `io.feishu-agent-dsh-consumer.service`
    （连字符），而 finalize/reload/verify/enable-reply 全用点号 `io.feishu-agent.dsh-consumer.service`。
    于是 reload-consumer.sh 判定"消费者未安装"→ 静默 exit 0，消费者留在旧 release 上，
    verify.sh 的 `consumer_release=not-installed` 又被 D/F 阶段当绿通过。
    """
    import re

    prefix = re.search(r'UNIT_PREFIX="([^"]+)"', INSTALLER).group(1)
    suffix = re.search(r'UNIT_NAME="\$UNIT_PREFIX(.*?)"', INSTALLER).group(1)
    canonical = prefix + suffix
    assert canonical == 'io.feishu-agent.dsh-consumer.service'
    for name in ('finalize-systemd.sh', 'reload-consumer.sh', 'verify.sh', 'enable-reply.sh',
                 'migrate-legacy-units.sh'):
        text = (ROOT / 'dist' / 'wsl2' / 'wsl' / name).read_text()
        if name == 'migrate-legacy-units.sh':
            # 迁移脚本按形态识别旧名（"不以 io.feishu-agent. 开头"），不该写死任何具体单元名。
            assert 'io.feishu-agent-dsh-consumer' not in text
            continue
        assert canonical in text, '%s 引用的消费者单元名与安装器不一致' % name
    # 连字符旧名不许再被创建（只允许出现在解释性注释里）
    assert '"$UNIT_PREFIX-dsh-consumer.service"' not in INSTALLER
    # unit 的 After/Wants 必须指向真实存在的 bridge 单元（点号），否则依赖是空的
    assert 'Wants=$UNIT_PREFIX.bridge.service' in INSTALLER
    assert '$UNIT_PREFIX-personal-agent.service' not in INSTALLER


def test_verify_reads_the_hold_schema_that_hold_py_actually_writes(tmp_path):
    """挂起中的机器升级：verify 的 hold 断言必须读 hold.py 写的键（`active`）。

    2026-11-04 装的那版 verify.sh 读的是 `held`，而 bridge/hold.py 的唯一 schema 是 `active`。
    后果不是"少报一条"：挂起状态下升级，F 阶段会拿到
    `hold=inconsistent(file=running mode=held)` → verify.sh 退出 3 → install-windows.ps1 返回 7，
    **已经装好并激活的新 release 被报成安装失败**（2026-11-05 真机：本机 check 输出即此形态）。
    """
    import json
    import subprocess
    import time

    home = tmp_path / 'home'
    state = home / '.feishu-personal-agent'
    state.mkdir(parents=True)
    (state / 'config.json').write_text('{"target_open_id": "ou_x"}')
    (state / 'installation.json').write_text('{"installed_at": %d}' % int(time.time()))
    (state / 'status.json').write_text(json.dumps({
        'pid': 123, 'authenticated': True, 'sending_enabled': True,
        'workers': {'collector': True, 'details': True, 'sender': True},
        'heartbeat_at': time.time(), 'last_search_at': time.time(),
        'running_release': 'abc', 'installed_release': 'abc',
        'mode': 'held', 'dropped_during_hold': 3,
    }))
    # 唯一 schema 见 bridge/hold.py：`active`（不是 `held`）
    (state / 'hold.json').write_text(json.dumps(
        {'schema': 1, 'active': True, 'since_ms': 1, 'by': 'feishu'}))

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'getent').write_text(
        '#!/usr/bin/env bash\necho "tester:x:1000:1000::%s:/bin/bash"\n' % home)
    (bindir / 'getent').chmod(0o755)
    (bindir / 'systemctl').write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = --user ]; then exit 1; fi\n'
        'if [ "$1" = show ]; then echo ""; exit 0; fi\n'
        'exit 0\n')
    (bindir / 'systemctl').chmod(0o755)
    (bindir / 'lark-cli').write_text('#!/usr/bin/env bash\nexit 1\n')
    (bindir / 'lark-cli').chmod(0o755)
    unit_dir = tmp_path / 'units'
    unit_dir.mkdir()

    result = subprocess.run(
        ['bash', str(ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh'), '--user', 'tester'],
        capture_output=True, text=True,
        env={'PATH': '%s:/usr/bin:/bin' % bindir, 'SYSTEMD_UNIT_DIR': str(unit_dir),
             'HOME': str(home)})
    assert 'VERIFY mode=held' in result.stdout, result.stdout
    assert 'VERIFY hold=dropped=3' in result.stdout, result.stdout
    assert 'hold=inconsistent' not in result.stdout, result.stdout


def _run_verify_with_status(tmp_path, workers):
    """跑一次 verify.sh，返回 (stdout, 退出码)。

    只关心 workers 断言：其余输入（config/installation/unit/lark-cli）按"最少能过"摆。
    """
    import json
    import subprocess
    import time

    home = tmp_path / 'home'
    state = home / '.feishu-personal-agent'
    state.mkdir(parents=True)
    (state / 'config.json').write_text('{"target_open_id": "ou_x"}')
    (state / 'installation.json').write_text('{"installed_at": %d}' % int(time.time()))
    (state / 'status.json').write_text(json.dumps({
        'pid': 123, 'authenticated': True, 'sending_enabled': True,
        'workers': workers,
        'heartbeat_at': time.time(), 'last_search_at': time.time(),
        'running_release': 'abc', 'installed_release': 'abc', 'mode': 'running',
    }))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'getent').write_text(
        '#!/usr/bin/env bash\necho "tester:x:1000:1000::%s:/bin/bash"\n' % home)
    (bindir / 'getent').chmod(0o755)
    (bindir / 'systemctl').write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = --user ]; then exit 1; fi\n'
        'if [ "$1" = show ]; then echo ""; exit 0; fi\n'
        'exit 0\n')
    (bindir / 'systemctl').chmod(0o755)
    (bindir / 'lark-cli').write_text('#!/usr/bin/env bash\nexit 1\n')
    (bindir / 'lark-cli').chmod(0o755)
    unit_dir = tmp_path / 'units'
    unit_dir.mkdir()
    result = subprocess.run(
        ['bash', str(ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh'), '--user', 'tester'],
        capture_output=True, text=True,
        env={'PATH': '%s:/usr/bin:/bin' % bindir, 'SYSTEMD_UNIT_DIR': str(unit_dir),
             'HOME': str(home)})
    return result.stdout, result.returncode


def test_verify_takes_the_worker_list_from_the_code_not_a_copy():
    """worker 名单的唯一事实在 `bridge/service.py:WORKER_NAMES`。

    2026-11-05 真机：旧包 `verify.sh` 断言 4 个 worker（含已删除的 analyst），新代码只有 3 个，
    于是「用旧包的文件验新代码」必假红。verifier 自己抄一份名单，就是同一事实的第二个答案。
    """
    body = (ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh').read_text()
    assert 'WORKER_NAMES' in body
    assert "('collector', 'details', 'sender')" not in body
    assert 'bridge/service.py' in body
    # 名单本身仍由代码定义，且 run() 用的是它（不是另抄一遍字面量）
    service = (ROOT / 'bridge' / 'service.py').read_text()
    assert "WORKER_NAMES = ('collector', 'details', 'sender')" in service
    assert 'for name in WORKER_NAMES:' in service


def _workers_lines(out):
    return [line for line in out.splitlines() if line.startswith('VERIFY workers=')]


def test_verify_flags_a_missing_worker(tmp_path):
    out, code = _run_verify_with_status(tmp_path, {'collector': True, 'details': True})
    assert _workers_lines(out) == ['VERIFY workers=missing=sender'], out
    assert code == 3


def test_verify_flags_a_worker_from_another_version(tmp_path):
    """status.json 里多出一个当前代码没有的 worker = 跑的不是这份代码。"""
    out, code = _run_verify_with_status(
        tmp_path, {'collector': True, 'details': True, 'sender': True, 'analyst': True})
    assert _workers_lines(out) == ['VERIFY workers=extra=analyst'], out
    assert code == 3


def test_verify_accepts_exactly_the_workers_the_code_runs(tmp_path):
    # 这个最小 fixture 在别的断言上本来就红（launcher/lark_login/system_unit/consumer），
    # 所以只钉 workers 这一行：它必须是 ok，而不是 fallback。
    out, _code = _run_verify_with_status(
        tmp_path, {'collector': True, 'details': True, 'sender': True})
    assert _workers_lines(out) == ['VERIFY workers=ok'], out


def test_doctor_checks_the_workers_the_code_actually_runs():
    """`doctor` 的 workers_complete 也不能自己抄一份名单。

    与 verify.sh 同一个事实（`bridge/service.py:WORKER_NAMES`）：worker 一增减，
    这里抄的那份就会把健康机器报成「缺失=...」。doctor 是本机人和自动化最常看的那一眼。
    """
    cli = (ROOT / 'bridge' / 'cli.py').read_text()
    assert "('collector', 'details', 'sender')" not in cli
    assert 'from .service import WORKER_NAMES' in cli


def test_worker_list_is_not_copied_anywhere_in_runtime_code():
    """零残留：运行时代码里不许再出现 worker 名单的第二份（定义在 service.py，消费方 import）。

    2026-11-05 一次收口时发现它被抄了四处：verify.sh、doctor（cli.py）、setup.sh 验收、
    以及 PowerShell 测试夹具。每抄一次，worker 增减就会有一条通道假红或假绿。
    """
    literal = "('collector', 'details', 'sender')"
    # service.py 是定义处，天然含这一行；其余任何文件都不许再出现
    targets = [p for p in (ROOT / 'bridge').rglob('*.py') if p.name != 'service.py']
    targets += [ROOT / 'dist' / 'wsl2' / 'wsl' / 'verify.sh',
                ROOT / 'dist' / 'wsl2' / 'setup.sh',
                ROOT / 'dist' / 'wsl2' / 'install-windows.ps1']
    offenders = [str(p.relative_to(ROOT)) for p in targets if literal in p.read_text(encoding='utf-8', errors='replace')]
    assert offenders == [], 'worker 名单又被抄到：%s（应 import bridge.service.WORKER_NAMES）' % offenders
    service = (ROOT / 'bridge' / 'service.py').read_text()
    assert service.count(literal) == 1, 'worker 名单的定义只能有一处'
