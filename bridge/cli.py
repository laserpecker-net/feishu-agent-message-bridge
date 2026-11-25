import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time

from .store import Store, reply_uuid
from . import help as help_manual
from . import hold as hold_switch
from . import memory
from . import metrics
from . import turns as turn_log
from .events_view import event_view, parse_payload
from . import host
from . import release as brelease

LABEL = host.LABEL
LAUNCHCTL = '/bin/launchctl'
LAUNCH_STATUS_TIMEOUT = 10
# launchd 的 ExitTimeOut 是 90s（bridge/host.py）：bootout 后最多要等它 SIGKILL，
# 等待时间必须比 90s 长，否则会在 launchd 还在收尾时就报“仍在停止中”。
UNLOAD_TIMEOUT_SECONDS = 120
UNLOAD_POLL_SECONDS = 0.2
COLLECTOR_LAG_LIMIT_SECONDS = 300
TEACH_SCAN_LAG_LIMIT_SECONDS = 300


def _launch_target():
    return 'gui/%s/%s' % (os.getuid(), LABEL)


def _top_level_value(stdout, key):
    """取 launchd print 输出里最外层（缩进最小）的同名键值。

    `launchctl print` 的输出里嵌套块（resource coalition 等）也有 `state = active`，
    只按“首个匹配”取会依赖 launchd 的排版顺序；这里按缩进选最外层，顺序无关。
    """
    pattern = re.compile(r'^([ \t]*)' + re.escape(key) + r' = ([^\n]+)', re.M)
    matches = pattern.findall(stdout)
    if not matches:
        return None
    _, value = min(matches, key=lambda item: len(item[0].expandtabs(8)))
    return value.strip()


def _coerce(value):
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def launch_status(timeout=LAUNCH_STATUS_TIMEOUT):
    """返回 `{'loaded': bool, 'label': LABEL, 'state'/'pid'/'last_terminating_signal': ...}`。

    查询本身失败（超时 / launchctl 不存在 / 非 GUI 会话）时 `loaded` 为 False 且带
    `error`，不抛裸异常；解析 `state`/`pid`/`last terminating signal`/`last exit code`
    （`last exit code` 可能是 `(never exited)` 这类非数字串，原样保留）。
    """
    target = _launch_target()
    try:
        result = subprocess.run([LAUNCHCTL, 'print', target], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as error:
        return {'loaded': False, 'label': LABEL, 'error': '%s: %s' % (type(error).__name__, error)}
    parsed = {'loaded': result.returncode == 0, 'label': LABEL}
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip()
        if detail:
            parsed['error'] = detail[:300]
        return parsed
    state = _top_level_value(result.stdout, 'state')
    if state is not None:
        parsed['state'] = _coerce(state)
    pid = _top_level_value(result.stdout, 'pid')
    if pid is not None:
        parsed['pid'] = _coerce(pid)
    signal = _top_level_value(result.stdout, 'last terminating signal')
    if signal is not None:
        parsed['last_terminating_signal'] = signal
    exit_code = _top_level_value(result.stdout, 'last exit code')
    if exit_code is not None:
        parsed['last_exit_code'] = _coerce(exit_code)
    return parsed


def _result_error(result):
    if result is None:
        return '命令未返回结果'
    detail = (result.stderr or result.stdout or '').strip()
    return detail[:500] if detail else 'exit code %s' % result.returncode


def _run_launchctl(args, timeout=60, retries=0, delay=0.5):
    """执行 launchctl 子命令；失败按 delay*2**n（0.5/1/2s...）退避重试，返回最后一次结果。"""
    attempts = max(0, retries) + 1
    result = None
    for attempt in range(attempts):
        result = subprocess.run([LAUNCHCTL] + [str(arg) for arg in args], capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result
        if attempt + 1 < attempts:
            time.sleep(delay * (2 ** attempt))
    return result


@contextlib.contextmanager
def _cli_lock(home):
    """变更类命令的进程间互斥锁（home/cli.lock）；冲突时直接给出可操作的中文提示。"""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    handle = (home / 'cli.lock').open('a+')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('另一个 feishu-bridge CLI 命令正在执行，请稍后重试')
        yield
    finally:
        handle.close()


def _atomic_json(path, value):
    """pid 唯一临时文件 + os.replace，避免固定 `config.tmp` 的并发截断（D06）。"""
    path = Path(path)
    temp = path.with_name('.%s.%s.tmp' % (path.name, os.getpid()))
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.chmod(0o600)
        temp.replace(path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _release_info(home):
    installed = _read_json(Path(home) / 'installation.json').get('release_id')
    running = _read_json(Path(home) / 'status.json').get('running_release')
    return (installed or None), (running or None)


def _update_pending(home):
    installed, running = _release_info(home)
    return bool(installed and running and installed != running)


def _collector_lag(status_data):
    collector = status_data.get('collector')
    if not isinstance(collector, dict):
        health = status_data.get('health')
        collector = health.get('collector') if isinstance(health, dict) else None
    if not isinstance(collector, dict):
        return None
    lag = collector.get('watermark_lag_s')
    return lag if isinstance(lag, (int, float)) else None


def _wait_unloaded(timeout=UNLOAD_TIMEOUT_SECONDS):
    """bootout 之后等它真的不在 launchd 里；间隔从 0.1s 指数退避到 1s，避免空转。"""
    deadline = time.monotonic() + timeout
    delay = UNLOAD_POLL_SECONDS
    while True:
        if not launch_status(timeout=LAUNCH_STATUS_TIMEOUT).get('loaded'):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(delay)
        delay = min(delay * 2, 1.0)


def _kickstart(target, kill=False):
    result = _run_launchctl((['kickstart', '-k'] if kill else ['kickstart']) + [target], timeout=60, retries=2)
    if result.returncode:
        raise SystemExit('LaunchAgent 已加载但无法启动：%s；请运行 feishu-bridge status 查看状态后重试'
                         % _result_error(result))


SYSTEMCTL = 'systemctl'
SYSTEMD_PROPERTIES = 'LoadState,ActiveState,SubState,MainPID,UnitFileState'


def _systemd_env():
    """systemctl --user needs a session bus; wsl.exe sessions often lack XDG_RUNTIME_DIR."""
    environment = dict(os.environ)
    environment.setdefault('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
    return environment


def _run_systemctl(args, timeout=60, scope='user'):
    """执行 systemctl；scope='system' 时走系统级（非 root 用 sudo -n，避免卡在密码提示）。"""
    command = [SYSTEMCTL] + (['--user'] if scope == 'user' else [])
    if scope != 'user' and os.geteuid() != 0:
        command = ['sudo', '-n'] + command
    try:
        return subprocess.run(command + [str(arg) for arg in args],
                              capture_output=True, text=True, timeout=timeout,
                              env=_systemd_env())
    except (OSError, subprocess.SubprocessError) as error:
        hint = ('systemctl --user 调用失败：%s: %s；WSL2 请确认发行版内 /etc/wsl.conf 的 [boot] 段'
                '已写 systemd=true（不是 %%USERPROFILE%%\\.wslconfig），执行 wsl --shutdown 后重试'
                % (type(error).__name__, error)) if scope == 'user' else (
                '系统级 systemctl 调用失败：%s: %s；需要 passwordless sudo（或 sudo -v 预热）'
                % (type(error).__name__, error))
        raise SystemExit(hint)


def _systemd_show(unit, timeout, scope):
    """一次 systemctl show；scope 为 'user'（systemctl --user）或 'system'。"""
    command = [SYSTEMCTL] + (['--user'] if scope == 'user' else [])
    try:
        result = subprocess.run(command + ['show', unit, '--property=' + SYSTEMD_PROPERTIES],
                                capture_output=True, text=True, timeout=timeout,
                                env=_systemd_env())
    except (OSError, subprocess.SubprocessError) as error:
        return {'loaded': False, 'unit': unit, 'scope': scope,
                'error': '%s: %s' % (type(error).__name__, error)}
    if result.returncode:
        return {'loaded': False, 'unit': unit, 'scope': scope,
                'error': (result.stderr or result.stdout or '').strip()[:300]}
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition('=')
        if key:
            values[key.strip()] = value.strip()
    pid = values.get('MainPID') or ''
    return {'loaded': values.get('LoadState') == 'loaded', 'unit': unit, 'scope': scope,
            'active': values.get('ActiveState') == 'active', 'state': values.get('SubState'),
            'pid': int(pid) if pid.isdigit() and int(pid) > 0 else None,
            'unit_file_state': values.get('UnitFileState')}


def systemd_status(unit, timeout=10):
    """launch_status 的 systemd 版：{loaded, active, state, pid, unit_file_state, scope}。

    WSL2 会把用户级 systemd 实例跟着最后一个会话一起回收，所以持久化部署常把服务装在
    系统级（/etc/systemd/system，User=pc）。只看 `systemctl --user` 会把这些部署误报成
    process_running=false —— 先问用户级，它没在跑再问系统级；两处都查不到才算没装。
    """
    user = _systemd_show(unit, timeout, 'user')
    if user.get('active'):
        return user
    system = _systemd_show(unit, timeout, 'system')
    if system.get('active') or not user.get('loaded'):
        return system
    return user


def _system_scope_installed(unit):
    """系统级是否装了同名单元（/etc/systemd/system/<unit>）。"""
    return Path('/etc/systemd/system', unit).exists()


def _lifecycle_systemd(command, service):
    """WSL2/Linux 的服务生命周期，语义与 launchd 的 stop/start/restart 对齐。

    WSL2 会在最后一个客户端断开时回收用户级 systemd 实例，持久化部署因此常装在系统级
    （/etc/systemd/system/<unit>，User=pc）。装了系统级就管系统级，否则管用户级；
    否则 start/restart 会把用户级单元又拉起来，和系统级抢同一个 home。
    """
    service = Path(service)
    scope = 'system' if _system_scope_installed(service.name) else 'user'
    label = 'sudo systemctl' if scope == 'system' else 'systemctl --user'
    if scope == 'user' and not service.exists():
        raise SystemExit('未找到 systemd 单元 %s；请重新运行 install.py' % service)
    if command in ('stop', 'restart'):
        result = _run_systemctl(['stop', service.name], scope=scope)
        if result.returncode:
            raise SystemExit('停止 systemd 服务失败：%s' % _result_error(result))
    if command in ('start', 'restart'):
        result = _run_systemctl(['daemon-reload'], scope=scope)
        if result.returncode:
            raise SystemExit('%s daemon-reload 失败：%s' % (label, _result_error(result)))
        result = _run_systemctl(['enable', service.name], scope=scope)
        if result.returncode:
            raise SystemExit('启用 systemd 服务失败：%s' % _result_error(result))
        action = 'restart' if command == 'restart' else 'start'
        result = _run_systemctl([action, service.name], scope=scope)
        if result.returncode:
            raise SystemExit('启动 systemd 服务失败：%s；请运行 %s status %s 查看原因'
                             % (_result_error(result), label, service.name))
        status = systemd_status(service.name)
        if not status.get('active'):
            raise SystemExit('服务已提交但未进入 active（%s）；请运行 %s status %s'
                             % (status.get('state') or status.get('error'), label, service.name))


def _lifecycle(command, home, domain, target, plist):
    """stop/start/restart 的唯一执行路径：卸载 → 等待 → 启用 → 加载/重启，失败时给出恢复指引。

    P1-06：bootout 之后任何一步失败都不再是裸 traceback —— bootstrap/start 失败会以
    SystemExit 输出「已卸载且重新加载失败 + 恢复命令」；调用方保证持有 cli.lock（P2-23）。

    启动时若 installation.json 与 status.json 的 release 不一致，说明 install.py 刚换过 release
    （plist/PATH/launcher 也可能一起变了）：launchd 不会重读磁盘上的 plist，因此这里走
    bootout+bootstrap 重载，而不是只 kickstart。
    """
    unload = command in ('stop', 'restart')
    if command == 'start' and launch_status().get('loaded') and _update_pending(home):
        unload = True
    if unload:
        bootout_error = None
        if launch_status().get('loaded'):
            result = _run_launchctl(['bootout', target], timeout=60)
            if result.returncode:
                bootout_error = _result_error(result)
        if not _wait_unloaded():
            if bootout_error:
                raise SystemExit('无法卸载 LaunchAgent：%s；任务仍在停止中。请稍后重试 feishu-bridge stop'
                                 % bootout_error)
            retry = 'stop' if command == 'stop' else 'start'
            raise SystemExit('launchd 任务仍在停止中（等待 %ds 超时）；请稍后重试 feishu-bridge %s'
                             % (UNLOAD_TIMEOUT_SECONDS, retry))
    if command in ('start', 'restart'):
        result = _run_launchctl(['enable', target], timeout=60, retries=2)
        if result.returncode:
            raise SystemExit('LaunchAgent 重新启用失败：%s；请检查 plist（%s）与权限后重试 feishu-bridge start'
                             % (_result_error(result), plist))
        status = launch_status()
        if status.get('loaded'):
            if command == 'restart':
                _kickstart(target, kill=True)
            elif not status.get('pid'):
                _kickstart(target)
        else:
            result = _run_launchctl(['bootstrap', domain, str(plist)], timeout=60, retries=2)
            if result.returncode:
                raise SystemExit('LaunchAgent 已卸载且重新加载失败：%s；请检查 plist/权限后重试 feishu-bridge start'
                                 % _result_error(result))
            status = launch_status()
            if command == 'restart' or not status.get('pid'):
                _kickstart(target)


RELEASE_NAME = re.compile(r'[0-9a-f]{16}\Z')


def _installed_releases(home):
    releases = Path(home) / 'releases'
    if not releases.is_dir():
        return []
    entries = [path for path in releases.iterdir()
               if path.is_dir() and not path.is_symlink() and RELEASE_NAME.match(path.name)]
    return sorted(entries, key=lambda path: path.stat().st_mtime, reverse=True)


def _rollback(home, argv):
    """把 current 切回一个已安装的旧 release；切完需要 restart 才真正生效。"""
    parser = argparse.ArgumentParser(
        prog='feishu-bridge rollback',
        description='切回旧 release（只改 current 与 installation.json）。旧 release 必须能读当前 schema：'
                    '升级会就地迁移 state.sqlite3 且没有 down 脚本，跨 schema 回滚要先退库'
                    '（docs/ops-remote-manage.md §4）')
    parser.add_argument('--to', metavar='RELEASE_ID', help='目标 release id；默认取最近安装的另一个 release')
    parser.add_argument('--list', action='store_true', help='只列出可回滚的 release，不做任何修改')
    args = parser.parse_args(argv)
    home = Path(home)
    link = home / 'current'
    current = link.resolve().name if link.is_symlink() else None
    entries = _installed_releases(home)
    if args.list:
        print(json.dumps({'current': current, 'releases': [path.name for path in entries]},
                         ensure_ascii=False, indent=2))
        return
    target = args.to or next((path.name for path in entries if path.name != current), None)
    if not target:
        raise SystemExit('没有可回滚的其它 release（当前 %s）；用 --list 查看' % (current or '未安装'))
    if not RELEASE_NAME.match(target):
        raise SystemExit('release id 格式不对（16 位小写 hex）：%s' % target)
    release = home / 'releases' / target
    if not release.is_dir():
        raise SystemExit('release 不存在：%s；用 feishu-bridge rollback --list 看可用版本' % release)
    digest = brelease.release_id(release)
    if digest != target:
        raise SystemExit('release %s 内容损坏（digest=%s），拒绝切换' % (target, digest))
    with _cli_lock(home):
        temp = home / 'current.new'
        if temp.is_symlink() or temp.exists():
            temp.unlink()
        temp.symlink_to(release, target_is_directory=True)
        temp.replace(link)
        receipt_path = home / 'installation.json'
        receipt = _read_json(receipt_path)
        receipt.update(release_id=target, rolled_back_at=int(time.time()), rolled_back_from=current,
                       source_sha256=brelease.release_digest(release),
                       files=brelease.file_hashes(brelease.runtime_files(release), root=release))
        _atomic_json(receipt_path, receipt)
    print(json.dumps({'ok': True, 'from': current, 'to': target,
                      'note': '切回旧 release 后必须 feishu-bridge restart 才生效；'
                              '旧 release 必须与当前 schema 兼容'},
                     ensure_ascii=False, indent=2))


def _print_status(home):
    data = _read_json(Path(home) / 'status.json')
    installed, running = _release_info(home)
    now = time.time()
    age = now - (data.get('heartbeat_at') or 0)
    unit = host.service_path(LABEL)
    if host.is_macos():
        launch = launch_status()
        data['launchd'] = launch
        pid, backend = launch.get('pid'), 'launchd'
    else:
        systemd = systemd_status(unit.name)
        # WSL2 持久部署装的是系统级单元（/etc/systemd/system）；这里报的必须是真正在管的那个，
        # 否则 status/doctor 会把用户级残留路径当成实际托管单元（排查时会看错文件）。
        effective = (Path('/etc/systemd/system') / unit.name) if _system_scope_installed(unit.name) else unit
        data.update(systemd=systemd, service_unit=str(effective),
                    service_scope=('system' if effective != unit else 'user'))
        unit = effective
        pid, backend = systemd.get('pid'), 'systemd'
    data.update(service_backend=backend, service_path=str(unit),
                heartbeat_age_seconds=round(age, 1), home=str(home),
                process_running=bool(pid and pid == data.get('pid') and age < 30),
                installed_release=installed,
                update_pending=bool(installed and running and installed != running))
    if not data.get('mode'):
        data['mode'] = 'held' if hold_switch.is_active(home) else 'running'
    lag = _collector_lag(data)
    healthy = bool(data['process_running'] and data.get('authenticated')
                   and now - (data.get('last_search_at') or 0) < 120)
    # 挂起是有意为之：收件"不健康"是预期，mode=held 才是正确读法。
    data['receiving_healthy'] = bool(healthy and data['mode'] != 'held'
                                     and (lag is None or lag < COLLECTOR_LAG_LIMIT_SECONDS))
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _self_chat(home, argv):
    """Configure opt-in self-chat command channels (owner's own @-mentions)."""
    parser = argparse.ArgumentParser(prog='feishu-bridge self-chat',
                                     description='配置「自己 @ 自己」也可触发回复的会话白名单（默认关闭）')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--add', action='append', default=[], metavar='CHAT_ID', help='加入白名单（可重复）')
    group.add_argument('--remove', action='append', default=[], metavar='CHAT_ID', help='移出白名单（可重复）')
    group.add_argument('--clear', action='store_true', help='清空白名单')
    parser.add_argument('--list', action='store_true', help='只读查看当前白名单')
    args = parser.parse_args(argv)
    home = Path(home)
    config_path = home / 'config.json'
    if not config_path.exists():
        raise SystemExit('未找到 %s；请先运行 install.py 安装 Feishu 桥接服务' % config_path)
    with _cli_lock(home):
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError) as error:
            raise SystemExit('配置文件 %s 无法解析：%s；请检查 JSON 或重新运行 install.py'
                             % (config_path, error))
        if not isinstance(config, dict):
            raise SystemExit('配置文件 %s 必须是 JSON 对象' % config_path)
        current = config.get('self_chat_ids')
        if isinstance(current, str):
            current = [current]
        ids = ({item for item in current if isinstance(item, str) and item.startswith('oc_')}
               if isinstance(current, list) else set())
        changed = False
        for chat_id in args.add:
            if not isinstance(chat_id, str) or not chat_id.startswith('oc_') or len(chat_id) < 4:
                raise SystemExit('无效 CHAT_ID：%r；应以 oc_ 开头' % chat_id)
            if chat_id not in ids:
                ids.add(chat_id)
                changed = True
        for chat_id in args.remove:
            if chat_id in ids:
                ids.remove(chat_id)
                changed = True
        if args.clear and ids:
            ids.clear()
            changed = True
        if changed:
            config['self_chat_ids'] = sorted(ids)
            _atomic_json(config_path, config)
    print(json.dumps({'self_chat_ids': sorted(ids), 'changed': changed,
                      'restart_required': changed}, ensure_ascii=False, indent=2))


def _bots(home, argv):
    """是否回复「别的机器人 @ 本人」的消息（默认回复；关闭可避免 AI 互刷刷爆每日配额）。"""
    parser = argparse.ArgumentParser(prog='feishu-bridge bots',
                                     description='控制是否回复其他机器人（sender_type=app）@ 本人的消息')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--disable', action='store_true', help='不回复别的机器人（写入 reply_to_bots=false）')
    group.add_argument('--enable', action='store_true', help='恢复回复别的机器人（删除该键，回到默认）')
    parser.add_argument('--list', action='store_true', help='只读查看当前设置')
    args = parser.parse_args(argv)
    home = Path(home)
    config_path = home / 'config.json'
    if not config_path.exists():
        raise SystemExit('未找到 %s；请先运行 install.py 安装 Feishu 桥接服务' % config_path)
    changed = False
    with _cli_lock(home):
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError) as error:
            raise SystemExit('配置文件 %s 无法解析：%s；请检查 JSON 或重新运行 install.py'
                             % (config_path, error))
        if not isinstance(config, dict):
            raise SystemExit('配置文件 %s 必须是 JSON 对象' % config_path)
        if args.disable:
            changed = config.get('reply_to_bots') is not False
            config['reply_to_bots'] = False
        elif args.enable:
            changed = 'reply_to_bots' in config
            config.pop('reply_to_bots', None)
        if changed:
            _atomic_json(config_path, config)
    enabled = config.get('reply_to_bots') is not False
    print(json.dumps({'reply_to_bots': enabled, 'changed': changed,
                      'restart_required': changed}, ensure_ascii=False, indent=2))


def _pause_resume(home, command):
    config_path = Path(home) / 'config.json'
    if not config_path.exists():
        raise SystemExit('未找到 %s；请先运行 install.py 安装 Feishu 桥接服务' % config_path)
    with _cli_lock(home):
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError) as error:
            raise SystemExit('配置文件 %s 无法解析：%s；请检查 JSON 或重新运行 install.py' % (config_path, error))
        if not isinstance(config, dict):
            raise SystemExit('配置文件 %s 必须是 JSON 对象' % config_path)
        config['sending_enabled'] = command == 'resume'
        _atomic_json(config_path, config)
    print('已%s自动分析和回复；收件继续（要连采集一起停，请用 feishu-bridge hold）。'
          % ('开启' if config['sending_enabled'] else '暂停'))


def _wait_release_applied(home, release_ms, timeout_seconds=3.0):
    """等服务把 hold.json 的恢复落到 store；返回本次挂起丢弃条数，超时返回 None。"""
    from .store import Store
    deadline = time.time() + timeout_seconds
    while True:
        try:
            store = Store(Path(home) / 'state.sqlite3')
            applied = store.meta('hold_release_applied_ms') or 0
            if applied >= release_ms:
                return store.meta('hold_dropped_current') or 0
        except Exception:  # 数据库暂不可用（服务正在迁移/首次安装）→ 走"未确认"分支
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def _hold(home, command, argv):
    """挂起开关：hold（下班，零采集/零入队/零回复） / release（上班，窗口内消息不补答）。

    状态写在 home/hold.json，服务端按 mtime 感知；两步都是幂等的。
    """
    parser = argparse.ArgumentParser(
        prog='feishu-bridge ' + command,
        description='挂起后不再采集、不再入队、不再回复；恢复时把关闭窗口从时间轴上切掉')
    parser.add_argument('-r', '--reason', default=None, help='挂起原因（只记录，最长 80 字）')
    parser.add_argument('--by', default='cli', help='操作者标识（默认 cli）')
    args = parser.parse_args(argv)
    if _update_pending(home):
        installed, running = _release_info(home)
        raise SystemExit('无法%s：已安装 %s 但进程还跑在 %s 上（update_pending）。\n'
                         '旧版进程不认挂起开关，现在切只会得到「看起来已下班」。先 feishu-bridge restart。'
                         % (('下班' if command == 'hold' else '上班'), installed, running))
    config_path = Path(home) / 'config.json'
    if not config_path.exists():
        raise SystemExit('未找到 %s；请先运行 install.py 安装 Feishu 桥接服务' % config_path)
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as error:
        raise SystemExit('配置文件 %s 无法解析：%s；请检查 JSON 或重新运行 install.py'
                         % (config_path, error))
    notice = None
    with _cli_lock(home):
        now = time.time()
        state = hold_switch.read_state(home)
        if command == 'hold':
            if state.get('active'):
                print('已在挂起中（自 %s），无需重复关闭。' % hold_switch.clock(state.get('since_ms')))
            else:
                payload = dict(hold_switch.default_state())
                payload.update({'active': True, 'since_ms': int(now * 1000), 'by': args.by,
                                'reason': (args.reason or '')[:80] or None})
                hold_switch.write_state(home, payload)
                notice = hold_switch.hold_notice(payload)
                print(notice)
                print('· 服务会在 1 秒内停止采集，并把排队中的消息作废')
                print('· 挂起是持久的：重启机器后仍然是挂起状态')
        else:
            if not state.get('active'):
                print('未处于挂起状态，无需恢复。')
            else:
                state.update({'active': False, 'last_release_at_ms': int(now * 1000),
                              'released_by': args.by})
                hold_switch.write_state(home, state)
                # 丢弃条数是**服务**在应用恢复时算的（sync_hold → drop_pending）。
                # 这里等它一拍再报数，保证终端回执与飞书里「上班」的回执逐字一致；
                # 服务没在跑时如实说"未确认"，绝不编一个"无积压"。
                dropped = _wait_release_applied(home, int(now * 1000))
                notice = hold_switch.release_notice(dropped, since=state.get('since_ms'))
                print(notice)
    from .service import self_chat_ids
    chats = sorted(self_chat_ids(config))
    if not chats:
        print('提示：未配置自聊控制频道，飞书里发「上班/下班」不会生效；'
              '用 feishu-bridge self-chat add <oc_...> 配置后可用。')
    else:
        if notice:
            _send_hold_notice(config, chats[0], notice)
        print('提示：也可以在自聊里发「上班」「下班」控制（约 30 秒内生效）。')
    return 0


def _latency(home, argv):
    """一条消息的三段耗时（采集 / 消费者 / 投递）。只读。

    为什么要一条命令而不是写 SQL：数字要能被复跑、被对比，不然"变快了"只是感觉。
    """
    parser = argparse.ArgumentParser(
        prog='feishu-bridge latency',
        description='最近若干条已答消息的耗时分解（采集 / 消费者 / 投递）')
    parser.add_argument('--window', type=int, default=metrics.DEFAULT_WINDOW,
                        help='用最近的多少条样本（默认 %d）' % metrics.DEFAULT_WINDOW)
    args = parser.parse_args(argv)
    print(json.dumps(metrics.latency(home, window=args.window), ensure_ascii=False, indent=2))
    return 0


def _turns(home, argv):
    """归因日志：每轮回复用了哪几条教材、结果如何。只读，不改任何东西。"""
    parser = argparse.ArgumentParser(
        prog='feishu-bridge turns',
        description='回复归因：每轮用了哪几条教材、成功还是失败（L2-b）')
    parser.add_argument('--limit', type=int, default=20, help='最多列多少条（默认 20）')
    parser.add_argument('--outcome', choices=turn_log.OUTCOMES, help='只看 sent/failed/timeout')
    parser.add_argument('--summary', action='store_true', help='只给统计，不列明细')
    args = parser.parse_args(argv)
    if args.summary:
        print(json.dumps(turn_log.summary(home), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(turn_log.turns(home, limit=args.limit, outcome=args.outcome),
                     ensure_ascii=False, indent=2))
    return 0


def _task(home, argv):
    """任务账本：跨消息的活做到哪一步、产物在哪、卡在哪个问题上。

    审批这一层（draft/approve/reject/note/expire）也在这里，因为它们改的是同一本账；
    **判断逻辑不在这里**——三态解析、拒绝词表、卡片渲染只在 ``bridge/approval.py`` 定义一次，
    这里只做参数校验、调用 store、把结果打成 JSON。
    """
    from . import approval

    parser = argparse.ArgumentParser(
        prog='feishu-bridge task', description='L1 任务账本：跨消息的活做到哪一步')
    actions = parser.add_subparsers(dest='action')
    listing = actions.add_parser('list', help='列出任务（默认全部，按最近更新排序）')
    listing.add_argument('--state', default=None,
                         choices=['open', 'running', 'blocked', 'done', 'failed', 'cancelled'])
    listing.add_argument('--limit', type=int, default=20)
    show = actions.add_parser('show', help='看一个任务的完整账本')
    show.add_argument('task_id')
    cancel = actions.add_parser('cancel', help='叫停一个任务（state=cancelled，历史保留）')
    cancel.add_argument('task_id')
    draft = actions.add_parser('draft', help='起草一张任务卡：建账（open）+ 私聊发给本人等批准')
    draft.add_argument('--chat', required=True, help='这张活是在哪个会话提出来的（oc_ 开头）')
    draft.add_argument('--origin-mid', required=True, help='哪条消息提出来的（审计用）')
    draft.add_argument('--question-event', type=int, default=None,
                       help='对应事件 id：给了就登记成待答求助，本人引用回复即可作答')
    draft.add_argument('--title', default=None, help='任务名（默认取【活】那一句）')
    draft.add_argument('--body-file', default='-', help='五段内容 JSON；默认 stdin')
    direct = actions.add_parser('direct', help='主人自己下的写活：免批准，直接派给干活端（不发卡）')
    direct.add_argument('--chat', required=True, help='这条活是在哪个会话下下来的（oc_ 开头）')
    direct.add_argument('--origin-mid', required=True, help='主人那条下令的消息 id（审计 + 幂等键）')
    direct.add_argument('--title', default=None, help='任务名（默认取【活】那一句）')
    direct.add_argument('--body-file', default='-', help='五段内容 JSON；默认 stdin')
    approve = actions.add_parser('approve', help='主人批准：open→running，原话整段进 hints')
    approve.add_argument('task_id')
    approve.add_argument('--mid', required=True, help='主人那条批准消息的 id（审计 + 幂等键）')
    approve.add_argument('--kind', required=True, choices=list(Store.APPROVAL_KINDS),
                         help='批准方式：quoted=引用回复，unique_pending=当时只有一张待批，command=私聊发「执行」')
    approve.add_argument('--executor', default=None, help='谁去干（S3a 的干活端填）')
    approve.add_argument('--body-file', default='-', help='主人原话；默认 stdin')
    reject = actions.add_parser('reject', help='主人不批：open→cancelled（历史保留）')
    reject.add_argument('task_id')
    reject.add_argument('--mid', required=True, help='主人那条拒绝消息的 id（审计 + 幂等键）')
    note = actions.add_parser('note', help='在账本上记一笔（不推进状态）')
    note.add_argument('task_id')
    note.add_argument('--body-file', default='-', help='要记的话；默认 stdin')
    expire = actions.add_parser('expire', help='把过点没批的卡作废（open→cancelled），并回群说一句')
    expire.add_argument('--ttl-minutes', type=float,
                        default=approval.CARD_TTL_SECONDS / 60.0,
                        help='多久算过点（默认 30 分钟）')
    expire.add_argument('--dry-run', action='store_true', help='只列出，不改状态、不发消息')
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 0
    home = Path(home)
    database = home / 'state.sqlite3'
    if not database.exists():
        raise SystemExit('未找到状态库 %s；请先运行 install.py' % database)
    store = Store(str(database))
    now = time.time()
    try:
        if args.action == 'list':
            tasks = store.tasks(args.state, args.limit)
            print(json.dumps({'count': len(tasks), 'tasks': [_task_view(task) for task in tasks]},
                             ensure_ascii=False, indent=2))
        elif args.action == 'show':
            task = store.get_task(args.task_id)
            if task is None:
                raise SystemExit('没有这个任务：%s' % args.task_id)
            print(json.dumps(_task_view(task), ensure_ascii=False, indent=2))
        elif args.action == 'cancel':
            task = store.set_task_state(args.task_id, 'cancelled', now)
            if task is None:
                raise SystemExit('没有这个任务：%s' % args.task_id)
            print(json.dumps(_task_view(task), ensure_ascii=False, indent=2))
        elif args.action == 'draft':
            return _task_draft(home, store, args, now)
        elif args.action == 'direct':
            return _task_direct(home, store, args, now)
        elif args.action in ('approve', 'reject', 'note'):
            return _task_decide(home, store, args, now)
        elif args.action == 'expire':
            return _task_expire(home, store, args, now)
    except ValueError as error:
        raise SystemExit('task %s 失败：%s' % (args.action, error))
    finally:
        store.close()
    return 0


def _task_draft(home, store, args, now):
    """建账 → 私聊发卡片 → 把卡片记成这条任务的消息。任何一步失败都不留"看不见的活"。"""
    from . import approval

    raw = _read_reply_body(args.body_file, 'task draft')
    try:
        payload = json.loads(raw)
    except ValueError:
        raise SystemExit('task draft 正文必须是 JSON：'
                         '{"summary","why_me","steps":[...],"touches":[...],"done_when"}')
    if not isinstance(payload, dict):
        raise SystemExit('task draft 正文必须是 JSON 对象')
    try:
        card = approval.render_card(summary=payload.get('summary'), why_me=payload.get('why_me'),
                                    steps=payload.get('steps'), touches=payload.get('touches'),
                                    done_when=payload.get('done_when'))
    except approval.CardError as error:
        # 起草失败不许落账：卡片写不成，说明这张活还没想清楚（计划 §2.1）。
        print(json.dumps({'ok': False, 'error': str(error), 'stage': 'render'},
                         ensure_ascii=False, indent=2))
        return 2
    title = args.title if isinstance(args.title, str) and args.title.strip() else payload.get('summary')
    # 卡片正文一起落账：批准后干活端的任务书取它，而不是群里那条来源消息。
    task = store.draft_task(args.chat, title, now, origin_mid=args.origin_mid,
                            message_id=args.origin_mid, card=card)
    config = _load_config(home)
    result = _send_owner_notice(home, config, card, 'task-card:' + task['id'],
                                question_event=args.question_event, needs='decision')
    if not result.get('ok') or not result.get('sent'):
        store.set_task_state(task['id'], 'cancelled', now)
        store.note_task(task['id'], '卡片没发出去，作废：%s'
                        % (result.get('note') or result.get('error') or '未知原因'), now)
        result.update({'task_id': task['id'], 'stage': 'send'})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    card_mid = result['message_id']
    # 卡片本身就是这条任务的一条消息（task_messages）：本人引用它时任务能对上号。
    # 不占 tasks 的任何一列——那些是审计字段，塞消息 id 进去会让"批准消息"和"卡片消息"混在一起。
    store.attach_task_message(task['id'], card_mid, now)
    result.update({'ok': True, 'task': _task_view(store.get_task(task['id'])),
                   'card_mid': card_mid, 'card_chars': len(card)})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _task_direct(home, store, args, now):
    """主人自己下的写活：建账 → **按 owner_direct 立即批准**（跳过发卡）→ 群里回一句。

    为什么有这条：2026-11-19 主人在群里下了一道写活（改 README 并提交），回话端直接替他
    改了仓库、还提交了 —— 没有批准、没有工作树、账本里没有痕迹。本人下令确实不必再问他一遍，
    但"谁干的、改了哪、能不能回滚"这三件事不能丢，所以免批的同时**必须**走干活端。

    护栏（一条都不许省）：
    1. 来源消息必须是**本人**发的、且不是 app 身份（别人的活只能在卡片里等他批）；
    2. ``touches`` 与 ``done_when`` 缺一就不派：没想清楚就先去问，不许猜；
    3. 同一条来源消息重复派活 → 返回既有任务（``duplicate=True``），不产生第二件活。
    """
    from . import approval

    raw = _read_reply_body(args.body_file, 'task direct')
    try:
        payload = json.loads(raw)
    except ValueError:
        raise SystemExit('task direct 正文必须是 JSON：'
                         '{"summary","why_me","steps":[...],"touches":[...],"done_when"}')
    if not isinstance(payload, dict):
        raise SystemExit('task direct 正文必须是 JSON 对象')
    touches = payload.get('touches')
    done_when = payload.get('done_when')
    if not touches or not done_when:
        print(json.dumps({'ok': False, 'stage': 'validate',
                          'error': '缺 touches 或 done_when：没想清楚就先在群里问一句，不许派活'},
                         ensure_ascii=False, indent=2))
        return 2
    config = _load_config(home)
    owner = config.get('target_open_id')
    source = store.get_message(args.origin_mid)
    if source is None:
        print(json.dumps({'ok': False, 'stage': 'guard', 'error': '找不到那条来源消息'},
                         ensure_ascii=False, indent=2))
        return 2
    if source.get('sender_type') == 'app' or source.get('sender_id') != owner:
        # app / 别人下的令只能走卡片：他们不能替主人做主（这条是权限边界，不是音量问题）。
        print(json.dumps({'ok': False, 'stage': 'guard',
                          'error': '只有本人自己下的活才能直接派；别人的活要等本人批准'},
                         ensure_ascii=False, indent=2))
        return 2
    existing = store.task_for_origin(args.origin_mid)
    if existing is not None:
        print(json.dumps({'ok': True, 'duplicate': True, 'task': _task_view(existing)},
                         ensure_ascii=False, indent=2))
        return 0
    try:
        card = approval.render_card(summary=payload.get('summary'),
                                    why_me=payload.get('why_me') or '你自己下的令，不用再问你一遍。',
                                    steps=payload.get('steps'), touches=touches, done_when=done_when)
    except approval.CardError as error:
        print(json.dumps({'ok': False, 'error': str(error), 'stage': 'render'},
                         ensure_ascii=False, indent=2))
        return 2
    title = args.title if isinstance(args.title, str) and args.title.strip() else payload.get('summary')
    task = store.draft_task(args.chat, title, now, origin_mid=args.origin_mid,
                            message_id=None, card=card)
    original_text = ' '.join(str(source.get('text') or '').split())[:2000]
    result = store.approve_task(task['id'], approval_mid=args.origin_mid,
                                approval_kind='owner_direct', hints_text=original_text,
                                now=now)
    if not result.get('ok'):
        store.set_task_state(task['id'], 'cancelled', now)
        print(json.dumps({'ok': False, 'stage': 'approve', 'error': '批准没落账，任务已作废'},
                         ensure_ascii=False, indent=2))
        return 2
    # 群里回一句（走唯一出口；挂在原消息下，不改 inbox 状态——这是补充说明，不是答复）。
    try:
        store.announce(now, '收到，这件我派给干活端了，干完回你。',
                       inbox_id=args.origin_mid, uuid='owner-direct:' + task['id'])
    except Exception as error:  # noqa: BLE001 - 回执发不出去不能把已派出去的活撤回来
        receipt = {'ok': False, 'error': '%s: %s' % (type(error).__name__, str(error)[:120])}
    else:
        receipt = {'ok': True}
    print(json.dumps({'ok': True, 'duplicate': False, 'task': _task_view(store.get_task(task['id'])),
                      'work_event_id': result.get('work_event_id'),
                      'work_queued': result.get('work_queued'), 'receipt': receipt},
                     ensure_ascii=False, indent=2))
    return 0


def _task_decide(home, store, args, now):
    """approve / reject / note 三条写账动作：参数校验在这里，语义在 store 与 approval。"""
    from . import approval

    if args.action == 'approve':
        text = _read_maybe_empty(args.body_file, 'task approve')
        verdict, hints = approval.parse_answer(text)
        if verdict != 'approve':
            print(json.dumps({'ok': False, 'verdict': verdict,
                              'reason': '这句话判成不批准就没法推进；不批请走 reject，空答复走追问'},
                             ensure_ascii=False, indent=2))
            return 4
        result = store.approve_task(args.task_id, approval_mid=args.mid, approval_kind=args.kind,
                                    hints_text=hints, now=now, executor=args.executor)
        if result['task'] is None:
            raise SystemExit('没有这个任务：%s' % args.task_id)
        print(json.dumps({'ok': result['ok'], 'applied': result['applied'],
                          'duplicate': result['duplicate'], 'task': _task_view(result['task'])},
                         ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 4
    if args.action == 'reject':
        result = store.reject_task(args.task_id, approval_mid=args.mid, now=now)
        if result['task'] is None:
            raise SystemExit('没有这个任务：%s' % args.task_id)
        print(json.dumps({'ok': result['ok'], 'applied': result['applied'],
                          'duplicate': result['duplicate'], 'task': _task_view(result['task'])},
                         ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 4
    text = _read_reply_body(args.body_file, 'task note')
    task = store.note_task(args.task_id, text, now)
    if task is None:
        raise SystemExit('没有这个任务：%s' % args.task_id)
    print(json.dumps(_task_view(task), ensure_ascii=False, indent=2))
    return 0


def _task_expire(home, store, args, now):
    """过点没批的卡作废，并在群里说一句（走 outbox 的 say，不绕过发送层）。"""
    from . import approval
    from .reply import owner_name_at, render_reply

    ttl_seconds = max(1.0, float(args.ttl_minutes) * 60.0)
    if args.dry_run:
        expired = [task for task in store.tasks('open', limit=500)
                   if approval.is_expired(task, now, ttl_seconds)]
        print(json.dumps({'dry_run': True, 'count': len(expired),
                          'tasks': [_task_view(task) for task in expired]},
                         ensure_ascii=False, indent=2))
        return 0
    config = _load_config(home)
    consumer = 'task-expire'
    store.register_consumer(consumer, 'other', now)

    def announce(task):
        body = render_reply('这张卡过点了，要办重新 @ 我。', config.get('signature'),
                            owner=owner_name_at(home))
        store.publish_say(consumer, now, body,
                          reply_uuid('task-expire:%s:%d' % (task['id'], int(now))),
                          chat_id=task['chat_id'])

    expired = approval.expire_cards(store, now, ttl_seconds=ttl_seconds, announce=announce)
    print(json.dumps({'ok': True, 'count': len(expired),
                      'tasks': [{'id': task['id'], 'title': task['title'],
                                 'chat_id': task['chat_id']} for task in expired]},
                     ensure_ascii=False, indent=2))
    return 0


def _task_view(task):
    """终端视角：账本原样 + 审计与"在等谁"。

    ``audit`` 是给审计看的四个字段（批准方式/批准消息/批准时间/执行者）与主人原话：
    批准方式只住在列里，不从 hints 里猜（计划 §2.3）。``waiting`` 只对 ``open`` 出现——
    "等谁批、等了多久"要能一眼看出来，否则任务卡过期了也没人知道。
    """
    view = dict(task)
    view['steps'] = [dict(step) for step in task.get('steps') or []]
    view['audit'] = {'approval_kind': task.get('approval_kind'),
                     'approval_mid': task.get('approval_mid'),
                     'approved_at': task.get('approved_at'),
                     'executor': task.get('executor'),
                     'hints': task.get('hints') or []}
    if task.get('state') == 'open':
        created = float(task.get('created_at') or 0)
        view['waiting'] = {'for': 'owner', 'seconds': max(0, int(time.time() - created))}
    return view


def _identity_transport(config):
    """按 ``identity.mode`` 造身份（doctor 用）。

    为什么不能直接用 ``Transport``：那是用户态实现；as_bot 下没有用户 token，
    用它体检必然报"未登录"（2026-11-21 真机：as_bot 的 doctor 误报 authenticated fail）。
    模式判断只在 ``identity.factory`` 里；这里把既有的 ``Transport`` 作为"默认实现"传进去，
    这样测试替换 ``Transport`` 的既有做法继续有效。
    """
    from .identity import factory as identity_factory
    return identity_factory.transport_for(
        config, threading.Event(), default_factory=lambda: _teach_transport(config))


def _teach_transport(config):
    """单独一个函数，方便测试替换成假 transport（不碰网络/子进程）。"""
    from .transport import Transport
    return Transport(config, threading.Event())


def _remind(home, argv):
    """提醒：登记、查看、取消（一次性；到点由服务心跳发出）。

    为什么提醒在 bridge 而不在消费者里"睡一觉再发"：消费者会话随时会被关掉、超时、
    换 release，睡在它进程里的定时器一定丢；服务是唯一长期活着的东西，而且它已经握着
    发送通道（``transport.send_text``）。代价是提醒只能发到会话（chat_id），不能 @ 人。
    """
    from . import remind as reminder
    from .store import Store

    parser = argparse.ArgumentParser(prog='feishu-bridge remind',
                                     description='提醒：一次性的到点提醒')
    actions = parser.add_subparsers(dest='action')
    add = actions.add_parser('add', help='登记一条提醒（同一来源消息只登记一次）')
    add.add_argument('--at', required=True, help='什么时候提醒：2026-11-06 21:40 或 21:40')
    add.add_argument('--text', required=True, help='提醒内容（例如"给张三打电话"）')
    add.add_argument('--chat', required=True, help='发到哪个会话（oc_ 开头；提醒不 @ 人）')
    add.add_argument('--message-id', required=True, help='来源消息 id（幂等键）')
    add.add_argument('--name', help='给本人看的短名称；缺省从内容截取')
    actions.add_parser('list', help='列出还没发的提醒')
    cancel = actions.add_parser('cancel', help='取消一条提醒')
    cancel.add_argument('key', help='提醒编号（数字）或名称')
    actions.add_parser('status', help='JSON：待发条数与最近失败')
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 0

    # 用和 service/其它命令同一个库：提醒必须落在服务读的那张表里，否则"设上了"只是
    # 写进了另一个文件（真机 2026-11-06 踩过：CLI 写 bridge.db，服务读 state.sqlite3，
    # 到点一声没响，`remind list` 还显示得好好的）。
    store = Store(Path(home) / 'state.sqlite3')
    try:
        if args.action == 'add':
            text = ' '.join(str(args.text).split())
            if not text:
                print(json.dumps({'ok': False, 'reason': '提醒内容是空的'}, ensure_ascii=False))
                return 0
            if len(text) > reminder.MAX_TEXT_CHARS:
                print(json.dumps({'ok': False, 'reason': '提醒内容超过 %d 字'
                                  % reminder.MAX_TEXT_CHARS}, ensure_ascii=False))
                return 0
            try:
                fire_at_ms = reminder.to_ms(args.at)
            except ValueError as error:
                print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
                return 0
            from .teach import auto_name, normalize_name
            name = normalize_name(args.name) or auto_name(text)
            row, duplicate = store.add_reminder(fire_at_ms, text, name, args.chat,
                                                args.message_id, int(time.time() * 1000))
            print(json.dumps({'ok': True, 'id': row['id'], 'name': row['name'],
                              'text': row['text'],
                              'fire_at': reminder.format_when(row['fire_at_ms']),
                              'fire_at_ms': row['fire_at_ms'],
                              'when': reminder.describe(row['fire_at_ms']),
                              'duplicate': duplicate}, ensure_ascii=False))
            return 0

        if args.action == 'cancel':
            row = store.reminder(args.key)
            if row is None:
                print(json.dumps({'ok': False, 'reason': '没有这条还没发的提醒：%s' % args.key},
                                 ensure_ascii=False))
                return 0
            store.cancel_reminder(row['id'], int(time.time() * 1000))
            print(json.dumps({'ok': True, 'id': row['id'], 'name': row['name'],
                              'text': row['text'],
                              'fire_at': reminder.format_when(row['fire_at_ms'])},
                             ensure_ascii=False))
            return 0

        if args.action == 'list':
            rows = store.reminders()
            now_ms = int(time.time() * 1000)
            print('待发提醒 %d 条' % len(rows))
            for row in rows:
                print('· 「%s」 %s · %s · %s' % (row['name'],
                                                reminder.format_when(row['fire_at_ms']),
                                                reminder.describe(row['fire_at_ms'], now_ms),
                                                row['text']))
            return 0

        # "最近的失败"要按**失败发生的顺序**取，不能按 fire_at（到点时刻）——提醒常常提前几天设，
        # 按 fire_at 排序取尾 5 条，看到的会是"最晚要响的那几条"，不是刚失败的那几条。
        failed = sorted((row for row in store.reminders(live_only=False)
                         if row['state'] == 'failed'),
                        key=lambda row: (row['fired_at_ms'] or 0, row['id']))[-5:]
        rows = store.reminders()
        print(json.dumps({'ok': True, 'pending': len(rows),
                          'next': reminder.format_when(rows[0]['fire_at_ms']) if rows else None,
                          'recent_failed': [{'id': row['id'], 'name': row['name'],
                                             'error': row['last_error']} for row in failed]},
                         ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


def _manual(argv):
    """打印使用说明书：正文只在 ``bridge/help.py`` 定义一次，飞书里发 help 也是这一份。"""
    parser = argparse.ArgumentParser(
        prog='feishu-bridge help', description='使用说明书（飞书私聊里发 help 得到同一份）')
    parser.add_argument('--json', action='store_true',
                        help='输出 {text, chunks}：正文与按段切好的多条')
    args = parser.parse_args(list(argv or []))
    if args.json:
        print(json.dumps({'text': help_manual.text(), 'chunks': help_manual.chunks()},
                         ensure_ascii=False, indent=2))
    else:
        print(help_manual.text())
    return 0


def _teach(home, argv):
    """教材：终端侧的查看、撤回、启用。

    串行化只靠 ``home/teach.lock``（服务采集时也拿同一把），不再叠一层 `cli.lock`：
    两层锁只会多一个锁序问题，不会多一份安全。
    """
    from . import teach as teach_log

    parser = argparse.ArgumentParser(
        prog='feishu-bridge teach',
        description='教材：在本人私聊里 @ 自己教它的话，这里查看与撤回')
    actions = parser.add_subparsers(dest='action')
    actions.add_parser('list', help='列出全部教材')
    show = actions.add_parser('show', help='看一条教材的全文与来源')
    show.add_argument('entry_id')
    revert = actions.add_parser('revert', help='撤回一条（追加 disable，不删历史）')
    revert.add_argument('entry_id', nargs='?',
                        help='条目编号（t-0007）或名称（「汇报先给结论」）；用 --last 撤回最后一条')
    revert.add_argument('--last', action='store_true', help='撤回最后一条仍然生效的条目')
    rename = actions.add_parser('rename', help='改一条教材的名称（正文一个字都不动）')
    rename.add_argument('entry_id', help='条目编号（t-0007）或现在的名称（「汇报先给结论」）')
    rename.add_argument('name', help='新名称（2~16 字，例如「汇报先给结论」）')
    actions.add_parser('reflect', help='复盘：打印最近几轮的归因（只读，不改教材）')
    enable = actions.add_parser('enable', help='启用教学（会校验会话是本人私聊）')
    enable.add_argument('--chat', help='本人私聊的 chat_id（oc_ 开头）')
    enable.add_argument('--self', action='store_true',
                        help='自动认出「自己跟自己」那个会话，不用手抄 chat_id')
    actions.add_parser('disable', help='停止接收新教材（已有教材仍然生效）')
    add = actions.add_parser('add', help='记一条教材（消费者判出"这是教材"时用，按消息 id 幂等）')
    add.add_argument('--kind', required=True, choices=sorted(teach_log.KINDS),
                     help='教材类型：原则/风格/反馈=准则，事实/资料/指针=只自己能知道的资料')
    add.add_argument('--text', required=True, help='教材正文（一句话）')
    add.add_argument('--name', help='给本人看的短名称（2~16 字，例如「汇报先给结论」）；缺省从正文截取')
    add.add_argument('--message-id', required=True, help='来源私聊消息 id（幂等键，重复调用不会记两条）')
    add.add_argument('--expose', choices=('say', 'know'), help='能不能原样说出；默认按类型推断')
    tidy = actions.add_parser('tidy', help='删完全重复 + 合并同一天相邻同类（不改写一个字）')
    tidy.add_argument('--dry-run', action='store_true', help='只报会怎么动，不写盘')
    actions.add_parser('status', help='JSON：版本、条数、错误')
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 0

    if args.action == 'add':
        try:
            result = teach_log.add_from_model(home, args.kind, args.text, args.message_id,
                                              expose=args.expose, name=args.name)
        except ValueError as error:
            # 退出码保持 0：消费者读的是 JSON，"没记上"的原因要能写进回复末尾的回执，
            # 而不是变成一个只有日志里才看得见的非零退出。
            print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
            return 0
        entry = result['entry']
        print(json.dumps({'ok': True, 'id': entry.get('id'), 'kind': entry.get('kind'),
                          'label': teach_log.KIND_LABEL.get(entry.get('kind'), entry.get('kind')),
                          'name': entry.get('name') or teach_log.auto_name(entry.get('text')),
                          'text': entry.get('text'), 'duplicate': result['duplicate']},
                         ensure_ascii=False))
        return 0

    if args.action == 'enable':
        if bool(args.self) == bool(args.chat):
            raise SystemExit('两种给法二选一：--chat <oc_…>（自己抄会话 id）'
                             '或 --self（自动认「自己跟自己」那个会话）')
        config = _read_json(Path(home) / 'config.json')
        if not config:
            raise SystemExit('未找到 %s/config.json；请先运行 install.py 安装' % home)
        transport = _teach_transport(config)
        if args.self:
            # 私聊必须显式登记才被采集，而"让人去飞书里翻 oc_…"是这道门槛上唯一的摩擦。
            # 这个会话是可算出来的（p2p 且对方就是本人），就不该让人手抄；认不准宁可报错。
            try:
                chat_id = transport.self_chat_id()
            except Exception as error:  # noqa: BLE001 - 认不出就把原因说清楚
                raise SystemExit('认不出「自己跟自己」的会话：%s' % error)
            print('自动认出的本人会话：%s' % chat_id)
        else:
            chat_id = str(args.chat or '').strip()
            if not chat_id.startswith('oc_'):
                raise SystemExit('--chat 要会话 chat_id（oc_ 开头），不是用户 open_id')
        try:
            info = transport.chat_info(chat_id)
        except Exception as error:  # noqa: BLE001 - 校验不成就别启用，把原因说清楚
            raise SystemExit('无法确认会话类型：%s' % error)
        mode = info.get('chat_mode') if isinstance(info, dict) else None
        if mode != 'p2p':
            raise SystemExit('这个会话不是私聊（chat_mode=%s），教学只能在本人私聊里用：'
                             '群里 @ 自己的话所有人都看得见，bridge 回复同事的内容也会混进教材。'
                             % mode)
        # 私聊消息的正文要靠 p2p 读权限才拿得到（检索用的是另一套 scope）。缺这条时行为是
        # "扫得到 id、取正文 access denied"：教学看着开着，其实一条也答不了——所以在这里就拒绝，
        # 把要补的授权命令原样打出来，别让人自己猜。
        missing = {'im:message.p2p_msg:get_as_user'} - transport.user_scopes()
        if missing:
            # 授权命令带着"已有 scope + 缺的这条"一起请求：只写缺的那一条会把别的授权挤掉，
            # 之后连群消息都读不了（identity() 的五条必需 scope 会直接不满足）。
            wanted = ' '.join(sorted(transport.user_scopes() | missing))
            raise SystemExit(
                '这个私聊的消息本条 token 读不到（已授权缺 %s）：启用后采集能搜到消息 id，'
                '取正文那一拍会被拒，表现成"教学开着、消息一条也答不了"。先把 scope 补上再启用：\n'
                '  lark-cli auth login --scope "%s"\n'
                '（应用后台没有声明这条权限时，先在开发者后台给应用加上再重试）'
                % (', '.join(sorted(missing)), wanted))
        teaching = {'schema': teach_log.SCHEMA, 'enabled': True,
                    'enabled_at_ms': int(time.time() * 1000), 'chat_id': chat_id,
                    'max_entries': teach_log.MAX_ENTRIES,
                    'max_item_chars': teach_log.MAX_ITEM_CHARS,
                    'max_inject_chars': teach_log.MAX_INJECT_CHARS}
        _atomic_json(teach_log.config_path(home), teaching)
        print('已启用教学：%s' % chat_id)
        print('· 在这个私聊里 @ 自己说一句就是教它；说「教材清单」看全部；'
              '想撤回某条就说「撤回 t-0005」（条目号在我每次记下时告诉你）')
        print(teach_log.PERMISSION_LINE)
        return 0

    if args.action == 'disable':
        current = teach_log.read_config(home)
        if current.get('corrupt'):
            raise SystemExit('%s 读不出来；先修好它再关闭（不会覆盖一个看不懂的配置）'
                             % teach_log.config_path(home))
        if not current:
            print('教学本来就没启用，无需关闭。')
            return 0
        current.update({'enabled': False})
        _atomic_json(teach_log.config_path(home), current)
        print('已停止接收新教材；已经教过的仍然生效（说「撤回 t-0005」才作废）。')
        return 0

    if args.action == 'status':
        config = teach_log.read_config(home)
        snapshot = teach_log.state(home)
        channel = teach_log.enabled_chat(home)
        reason = 'on'
        if not channel:
            # 默认开通之后，"没开"必须带原因，否则这台机器上没人知道下一步该做什么。
            try:
                scopes = _teach_transport(_read_json(Path(home) / 'config.json') or {}).user_scopes()
            except Exception:  # noqa: BLE001 - 只影响原因说明，不影响其它字段
                scopes = None
            first = None
            try:
                first = int(Store(Path(home) / 'state.sqlite3').meta('self_channel_first_try_ms') or 0) or None
            except Exception:  # noqa: BLE001 - 读不到就只少一个原因细分，不影响其它字段
                first = None
            waited = max(0.0, time.time() - first / 1000.0) if first else None
            reason = teach_log.channel_reason(config, scopes, waited)
        print(json.dumps({'enabled': bool(channel),
                          'chat_id': config.get('chat_id'),
                          'default_on': not config.get('corrupt') and config.get('enabled') is not False,
                          'reason': reason,
                          'corrupt': bool(config.get('corrupt')),
                          'rev': snapshot['rev'],
                          'charter_count': len(snapshot['charter']),
                          'notes_count': len(snapshot['notes']),
                          'bad_lines': snapshot['bad_lines'],
                          'pending_questions': len(teach_log.pending_questions(home)),
                          'memory_index': memory.status(home),
                          'error': snapshot['error']}, ensure_ascii=False, indent=2))
        return 0

    if args.action == 'tidy':
        print(json.dumps(teach_log.tidy(home, dry_run=args.dry_run), ensure_ascii=False, indent=2))
        return 0

    if args.action == 'rename':
        entry = teach_log.rename(home, args.entry_id, args.name)
        if entry is None:
            print(json.dumps({'ok': False,
                              'reason': '没有这条教材（或已经撤回）：%s' % args.entry_id},
                             ensure_ascii=False))
            return 0
        print(json.dumps({'ok': True, 'id': entry['id'],
                          'name': entry.get('name') or teach_log.auto_name(entry.get('text')),
                          'text': entry.get('text')}, ensure_ascii=False))
        return 0

    if args.action == 'list':
        snapshot = teach_log.state(home)
        entries = snapshot['charter'] + snapshot['notes']
        print('教材共 %d 条（版本 %d）' % (len(entries), snapshot['rev']))
        for entry in entries:
            print('· 「%s」 %s · %s · %s：%s' % (
                entry.get('name') or teach_log.auto_name(entry['text']),
                teach_log.KIND_LABEL.get(entry['kind'], entry['kind']), entry['id'],
                teach_log.EXPOSE_LABEL.get(entry['expose'], entry['expose']), entry['text']))
        if snapshot['error']:
            print('· 注意：%s' % snapshot['error'])
        if not teach_log.enabled_chat(home):
            print('· 教学当前未启用（feishu-bridge teach enable --chat <oc_...>）')
        return 0

    if args.action == 'show':
        entry = teach_log.find_entry(home, args.entry_id)
        if entry is None:
            raise SystemExit('没有这条教材：%s（用 feishu-bridge teach list 看现有条目）' % args.entry_id)
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        if entry.get('superseded'):
            print('· 这条已经被归并掉了：内容仍在合并后的条目里，历史保留可回退')
        elif entry.get('disabled'):
            print('· 这条已经撤回：不再生效，历史保留')
        return 0

    if args.action == 'reflect':
        from . import turns as turn_log
        report = turn_log.reflection(home)
        print(report['report'])
        return 0

    snapshot = teach_log.state(home)
    entries = snapshot['charter'] + snapshot['notes']
    if args.last:
        args.entry_id = (entries or [{}])[-1].get('id')
        if not args.entry_id:
            print(json.dumps({'ok': False, 'reason': '教材是空的，没有可撤回的'}, ensure_ascii=False))
            return 0
    # 编号和名称都收：本人说「撤回 汇报先给结论」，模型说 `teach revert t-0006`，同一份实现。
    entry_id = teach_log.live_entry_id(home, args.entry_id)
    entry = next((row for row in entries if row['id'] == entry_id), None) if entry_id else None
    if entry is None:
        print(json.dumps({'ok': False,
                          'reason': '没有这条教材（或已经撤回）：%s' % args.entry_id},
                         ensure_ascii=False))
        return 0
    teach_log.disable(home, entry_id)
    print(json.dumps({'ok': True, 'id': entry['id'], 'kind': entry.get('kind'),
                      'label': teach_log.KIND_LABEL.get(entry.get('kind'), entry.get('kind')),
                      'name': entry.get('name') or teach_log.auto_name(entry.get('text')),
                      'text': entry.get('text')}, ensure_ascii=False))
    return 0


def _send_hold_notice(config, chat_id, text):
    """把开关状态发到飞书自聊（S1 状态面）。失败只提示，绝不影响开关本身。"""
    from .transport import Transport
    try:
        Transport(config, threading.Event()).send_text(chat_id, text)
        print('· 已把状态回执发到飞书自聊')
    except Exception as error:  # noqa: BLE001 - 回执是尽力而为，任何失败都不该回滚开关
        print('· 飞书回执发送失败（开关已生效）：%s' % str(error)[:200])


def _log_files(home):
    """按重要性返回可以看的日志：bridge.log 是业务日志，launchd/systemd.*.log 是解释器/监管层日志
    （导入错误、进程被 KILL 这些只在后者里）。"""
    names = ['bridge.log', 'launchd.stdout.log', 'launchd.stderr.log', 'systemd.stdout.log',
             'systemd.stderr.log', 'bridge.out.log', 'bridge.err.log']
    files = [home / name for name in names if (home / name).exists()]
    files.extend(sorted(path for path in home.glob('*.log') if path not in files))
    return files


def _logs(home, argv=(), follow=False):
    parser = argparse.ArgumentParser(prog='feishu-bridge logs',
                                     description='看 bridge 日志（默认最近 80 行；--all 连后端日志一起看）')
    parser.add_argument('-n', '--lines', type=int, default=80, help='每个文件打印末尾多少行（默认 80）')
    parser.add_argument('-f', '--follow', action='store_true', help='持续跟踪（等价 tail -f）')
    parser.add_argument('--all', dest='all_files', action='store_true',
                        help='连 launchd/systemd 的 stdout/stderr 一起打印（查崩溃用）')
    options = parser.parse_args(list(argv))
    home = Path(home)
    if not home.exists():
        raise SystemExit('未找到状态目录 %s；请先运行 install.py 安装 Feishu 桥接服务' % home)
    main_log = home / 'bridge.log'
    files = _log_files(home) if options.all_files else [main_log]
    if not files:
        print('暂无日志（%s 不存在）' % main_log)
        return 0
    for path in files:
        if len(files) > 1:
            print('===== %s =====' % path.name)
        try:
            lines = path.read_text(errors='replace').splitlines()[-max(1, options.lines):]
        except OSError as error:
            print('（无法读取 %s：%s）' % (path, error))
            continue
        print('\n'.join(lines) if lines else '（空）')
    if follow:
        try:
            subprocess.run(['tail', '-f', str(main_log)])
        except (OSError, KeyboardInterrupt):
            return 130
    return 0


#: 有 message 事件积压、且超过这么久没人领取 → doctor 判 consumer_offline。
CONSUMER_LAG_LIMIT_SECONDS = 120


def _consumer_busy_since(home, now):
    """最早一条待处理 message 事件已经等了多久；没有积压返回 None。"""
    database = Path(home) / 'state.sqlite3'
    if not database.exists():
        return None
    # 只读打开：doctor 是体检，不许因为顺手打开 Store 而跑迁移/拿写锁。
    try:
        connection = sqlite3.connect('file:%s?mode=ro' % database, uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            """SELECT MIN(created_at) FROM events WHERE kind='message'
               AND state IN ('pending','leased')""").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    oldest = row[0] if row else None
    return max(0.0, now - float(oldest)) if oldest else None


def _doctor_checks(report, status_data, config, service_running, consumer_lag_s=None, home=None):
    """把体检结论收敛成「可自动判定的检查项」，让 macOS / Linux 共用同一套退出码语义。"""
    checks = []

    def add(name, ok, detail, level='fail'):
        checks.append({'name': name, 'status': 'ok' if ok else level, 'detail': detail})

    add('service_running', service_running,
        'pid=%s' % (report.get('pid') or '未取到') if service_running else '服务没在跑（或 pid 与心跳不一致）')
    age = report.get('heartbeat_age_seconds')
    add('heartbeat_fresh', isinstance(age, (int, float)) and age < 90,
        '心跳 %ss 前' % (round(age, 1) if isinstance(age, (int, float)) else '未知'))
    identity = report.get('identity') or {}
    token_status = str(identity.get('tokenStatus') or '').lower()
    # 认证判据由身份层给（as_user 判用户 token；as_bot 判 bot 身份 + token + 回环网关）。
    from .identity.factory import doctor_checks as identity_doctor_checks
    checks.extend(identity_doctor_checks(config or {}, home, identity))
    # 回复里"以谁的名义说话"只看一个来源：这台机器当前登录的飞书账号（report.identity 是
    # transport.identity() 校验过 openId 的那份）。代码/config 里都没有人名默认值——别人机器
    # 上静默继承仓库作者的名字是踩过的真实事故（HISTORY §4）。
    owner = ' '.join(str((identity or {}).get('userName')
                        or (identity or {}).get('label') or '').split())
    # 署名也由账号身份渲染：模板来自 config.json（可含 {owner}），没配就用内置模板；
    # 这里打印的是**最终会发出去的那一行字**，不是原始模板。
    from .reply import resolve_signature
    signature_template = (config or {}).get('signature')
    reply_signature = resolve_signature(signature_template, owner)
    signature_source = ('配置' if isinstance(signature_template, str) and signature_template.strip()
                        else '内置模板')
    checks.append({'name': 'reply_identity', 'status': 'ok' if owner else 'warn',
                   'detail': ('自称「%s的 AI 助手」（来源：本机飞书账号身份）/ 署名「%s」（%s）'
                              % (owner, reply_signature, signature_source)) if owner else
                             '取不到本机飞书账号身份（status.json 的 identity）：正文会用中性自称'
                             '「你是这个飞书账号本人的 AI 助手」，署名里的 {owner} 会回落成'
                             '「账号本人」，先确认 lark-cli 已登录'})
    add('release_match', not report.get('update_pending'),
        'installed=%s running=%s' % (report.get('release_id'), report.get('running_release')), level='warn')
    # 唯一的大脑是外部消费者：有积压事件却没人来领，就是硬故障（不是 warn）。
    lag = consumer_lag_s
    offline = lag is not None and lag > CONSUMER_LAG_LIMIT_SECONDS
    add('consumer_offline', not offline,
        ('message 事件已积压 %ss 无人领取（消费者没在跑？看 `feishu-bridge consumer status`）'
         % round(lag, 1)) if offline else
        ('外部消费者在线（无积压）' if lag is None else '外部消费者在线（积压 %ss）' % round(lag, 1)))
    # worker 是「服务真的在干活」的最小证据（Linux 侧 verify.sh 同样断言）。名单只能有一个
    # 答案：`bridge/service.py:WORKER_NAMES`（run() 用它起线程、verify.sh 从它读）。
    workers = status_data.get('workers')
    if not isinstance(workers, dict) or not workers:
        checks.append({'name': 'workers_complete', 'status': 'warn',
                       'detail': 'status.json 未上报 workers（旧版 release？）'})
    else:
        from .service import WORKER_NAMES  # 延迟导入：cli 顶层不该为一份名单拖进整个 service
        missing = [name for name in WORKER_NAMES if not workers.get(name)]
        add('workers_complete', not missing,
            '缺失=%s' % ','.join(missing) if missing else '%d 个 worker 都在' % len(WORKER_NAMES),
            level='fail')
    # 积压/放弃的数据不会让进程看起来「不健康」，但那是人真正要处理的：只报不判死。
    attention_data = report.get('attention') or {}
    inbox_bad = int(attention_data.get('inbox_needs_attention') or 0)
    outbox_bad = int(attention_data.get('outbox_needs_attention') or 0)
    dead = int(attention_data.get('events_dead') or 0)
    # 身份行：一台机器一种身份（as_user / as_bot），配置矛盾时这里是 fail 而不是静默
    try:
        from .identity import build_identity as _build_identity
        _identity = _build_identity(config)
        for _name, _value, _ok in _identity.describe():
            checks.append({'name': _name,
                           'status': 'ok' if _ok else 'fail',
                           'detail': _value})
    except Exception as _error:  # noqa: BLE001 - 配置矛盾就是 fail，不能空转
        checks.append({'name': 'identity', 'status': 'fail', 'detail': str(_error)[:200]})
    # bot 模式多一条硬判据：长连接收消息器（sidecar）必须真的在跑。
    # 只报 describe() 里的"通道是什么"不够——单元没起来时那条行仍然是 ok。
    try:
        from .identity import identity_mode as _identity_mode
        if _identity_mode(config) == 'bot':
            from . import host as _host
            _state = _host.service_state(_host.IDENTITY_SIDECAR_LABEL)
            checks.append({'name': 'identity_sidecar',
                           'status': 'ok' if _state.get('loaded') else 'fail',
                           'detail': 'unit=%s loaded=%s pid=%s'
                                     % (_host.IDENTITY_SIDECAR_LABEL, _state.get('loaded'),
                                        _state.get('pid'))})
    except Exception as _error:  # noqa: BLE001
        checks.append({'name': 'identity_sidecar', 'status': 'fail', 'detail': str(_error)[:200]})
    if inbox_bad or outbox_bad or dead:
        checks.append({'name': 'attention_clear', 'status': 'warn',
                       'detail': 'inbox_needs_attention=%d outbox_needs_attention=%d events_dead=%d'
                                 % (inbox_bad, outbox_bad, dead)})
    else:
        checks.append({'name': 'attention_clear', 'status': 'ok', 'detail': '无待处理/无放弃事件'})
    oldest = attention_data.get('oldest_attention_at')
    if isinstance(oldest, (int, float)) and oldest:
        hours = round((time.time() - oldest) / 3600.0, 1)
        if hours >= 1:
            checks.append({'name': 'backlog_age', 'status': 'warn',
                           'detail': '最老待处理项积压 %.1f 小时' % hours})
    teach = status_data.get('teach_channel') or {}
    if teach.get('enabled'):
        lag = teach.get('watermark_lag_s')
        fresh = isinstance(lag, (int, float)) and lag < TEACH_SCAN_LAG_LIMIT_SECONDS
        add('teach_scan', fresh,
            '教材私聊在扫描（落后 %ss）' % lag if fresh else
            '教学开着，但教材私聊的扫描落后 %ss（私聊消息会延迟或收不到）' % lag, level='warn')
    else:
        # 私聊通道默认开通，所以"没开"必须给出这台机器上唯一能做的下一步。
        reason = teach.get('reason')
        if reason == 'disabled':
            checks.append({'name': 'teach_channel', 'status': 'info',
                           'detail': '私聊通道是你自己关掉的（teach disable）；想再收私聊消息就 '
                                     'feishu-bridge teach enable --self'})
        elif reason == 'no_p2p_scope':
            add('teach_channel', False,
                '私聊通道没开：token 缺 im:message.p2p_msg:get_as_user（读私聊正文的权限）。'
                '补上后会自动开通，不用再登记：lark-cli auth login --scope "... '
                'im:message.p2p_msg:get_as_user ..."', level='warn')
        elif reason == 'detecting':
            checks.append({'name': 'teach_channel', 'status': 'info',
                           'detail': '正在认本人私聊会话（服务启动后一分钟内自动完成）'})
        elif reason == 'no_self_chat':
            add('teach_channel', False,
                '这台账号还没有「自己跟自己」的会话：在飞书里给自己发一条消息'
                '（搜自己的名字点进去，或「我的空间」），一分钟内私聊通道会自动开通', level='warn')
        elif reason == 'corrupt':
            add('teach_channel', False,
                'teach.json 读不出来：私聊通道不会自动开。修好或删掉 Application Support 下'
                '的 teach.json 再重启服务', level='warn')
        else:
            add('teach_channel', False,
                '这台机器的 status.json 里没有私聊通道状态（服务还在跑旧版本？）', level='warn')
    if report.get('mode') == 'held':
        checks.append({'name': 'mode', 'status': 'info',
                       'detail': '已挂起（held）：不收消息是有意为之'})
    return checks


def _doctor(home, service, argv=()):
    from .transport import Transport, TransportError
    parser = argparse.ArgumentParser(prog='feishu-bridge doctor',
                                     description='一次读全：服务/心跳/身份/版本/挂起/agent 可用性（--assert 用退出码断言）')
    parser.add_argument('--assert', dest='assert_fail', action='store_true',
                        help='有 fail 级检查项时退出码 1（供远程巡检/自动化使用）')
    options = parser.parse_args(list(argv))
    home = Path(home)
    installed, running = _release_info(home)
    status = _read_json(home / 'status.json')
    stats = status.get('stats')
    config = _read_json(home / 'config.json')
    now = time.time()
    age = now - (status.get('heartbeat_at') or 0)
    if host.is_macos():
        launch = launch_status()
        pid = launch.get('pid')
        report = {'home': str(home), 'service_backend': 'launchd', 'service': str(service),
                  'launchd': launch, 'plist': str(service), 'pid': pid}
    else:
        system_unit = Path('/etc/systemd/system') / Path(service).name
        effective = system_unit if _system_scope_installed(Path(service).name) else Path(service)
        systemd = systemd_status(effective.name)
        pid = systemd.get('pid')
        report = {'home': str(home), 'service_backend': 'systemd', 'service': str(effective),
                  'service_path': str(effective),
                  'service_scope': ('system' if effective != Path(service) else 'user'),
                  'systemd': systemd, 'pid': pid}
    service_running = bool(pid and pid == status.get('pid') and age < 30)
    report.update({
        'release_id': installed, 'running_release': running,
        'update_pending': bool(installed and running and installed != running),
        'heartbeat_age_seconds': round(age, 1) if status.get('heartbeat_at') else None,
        'process_running': service_running,
        'attention': stats.get('attention') if isinstance(stats, dict) else None,
        'mode': status.get('mode') or ('held' if hold_switch.is_active(home) else 'running'),
        'hold': {key: status.get(key) for key in ('held_since', 'held_by', 'held_reason',
                                                  'last_release_at', 'dropped_during_hold')},
    })
    consumer_lag_s = _consumer_busy_since(home, now)
    report['consumer_lag_seconds'] = consumer_lag_s
    if not config:
        report['error'] = '未找到或无法解析 %s；请先运行 install.py' % (home / 'config.json')
    else:
        try:
            transport = _identity_transport(config)
            report['identity'] = transport.identity()
            report['joined_groups'] = len(transport.inventory())
            report['sending_enabled'] = config.get('sending_enabled')
        except (TransportError, OSError, ValueError, KeyError, TypeError) as error:
            detail = '%s: %s' % (type(error).__name__, str(error)[:300])
            report['identity'] = {'available': False, 'error': detail}
            report['error'] = detail
    checks = _doctor_checks(report, status, config, service_running, consumer_lag_s, home=home)
    report['checks'] = checks
    report['verdict'] = 'fail' if any(item['status'] == 'fail' for item in checks) else 'ok'
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if (options.assert_fail and report['verdict'] == 'fail') else 0


def _question_summary(body):
    """从求助正文里取「一句话问题」用于登记：第一行里【需要你出手】后面的部分。"""
    for line in str(body or '').splitlines():
        line = line.strip()
        if not line:
            continue
        return line.replace('【需要你出手】', '').strip()[:200]
    return ''


def _load_config(home):
    """读 config.json（``notify`` 与 ``task draft`` 共用；坏了就大声失败，不静默降级）。"""
    config_path = Path(home) / 'config.json'
    if not config_path.exists():
        raise SystemExit('未找到 %s；请先运行 install.py 安装 Feishu 桥接服务' % config_path)
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as error:
        raise SystemExit('配置文件 %s 无法解析：%s' % (config_path, error))
    if not isinstance(config, dict):
        raise SystemExit('配置文件 %s 必须是 JSON 对象' % config_path)
    return config


def _send_owner_notice(home, config, body, key=None, question_event=None, needs=None,
                       dry_run=False):
    """把一条私聊通知发出去，必要时登记待答。``notify`` 与 ``task draft`` 共用这一条路。

    为什么仍绕过 outbox：桥接挂起时 sender 是停的，「已经挂起了」「这张卡要你批」这类状态
    通知要是也排队，就永远发不出去——这是设计稿 §2 记的唯一例外，S2 只是多用一次，不是新开通道。

    去重与限额在 ``bridge/notify.py``：同一件事 6 小时内只发一次、每小时上限（默认 6）。
    """
    from .reply import owner_name_at, render_reply
    from . import notify as notice
    from .transport import Transport, TransportError

    open_id = config.get('target_open_id')
    if not isinstance(open_id, str) or not open_id.startswith('ou_'):
        raise SystemExit('config.json 里的 target_open_id 不是用户 open_id：无法确定这条私聊发给谁')
    if config.get('sending_enabled') is not True:
        raise SystemExit('发送已暂停（feishu-bridge pause）：不发任何私聊')
    body = render_reply(body, config.get('signature'), owner=owner_name_at(home))
    key = key if isinstance(key, str) and key.strip() else notice.body_key(body)
    cap = config.get('notify_hourly_cap')
    cap = cap if isinstance(cap, int) and cap > 0 else notice.MAX_PER_HOUR
    state = notice.read_state(home)
    decision, reason = notice.decide(state, key, cap=cap)
    if dry_run:
        return {'ok': True, 'dry_run': True, 'decision': decision, 'dedupe_key': key,
                'target': 'owner'}
    if decision != 'send':
        return {'ok': True, 'sent': False, 'decision': decision, 'dedupe_key': key, 'note': reason}
    try:
        result = Transport(config, threading.Event()).send_dm(open_id, body, dedupe_key=key)
    except (TransportError, OSError, ValueError, subprocess.SubprocessError) as error:
        return {'ok': False, 'error': '%s: %s' % (type(error).__name__, str(error)[:300])}
    _atomic_json(notice.state_path(home), notice.record(state, key, chat_id=result.get('chat_id')))
    question = None
    if question_event is not None:
        from . import teach as teach_log
        try:
            question = teach_log.register_question(
                home, question_event, _question_summary(body), needs,
                result['message_id'], result.get('chat_id'))
        except ValueError as error:
            return {'ok': False, 'sent': True, 'error': str(error),
                    'message_id': result['message_id']}
    return {'ok': True, 'sent': True, 'message_id': result['message_id'],
            'chat_id': result.get('chat_id'), 'dedupe_key': key,
            'question': question['qid'] if question else None}


def _outbox(home, argv):
    """出站行的运维口子：现在只有「把被误判的终回复重判回来」这一件。

    为什么要有它：判据写错的时候，行已经发出去了，只是被标成 needs_attention。
    修好判据之后历史行不能手改库——要么重判通过，要么原样留着，两条路都可复跑。
    """
    parser = argparse.ArgumentParser(prog='feishu-bridge outbox',
                                     description='出站行运维：重判被误判的终回复')
    commands = parser.add_subparsers(dest='command', required=True)
    item = commands.add_parser('reverify', help='重判 needs_attention 的终回复（只认回读/提及两类误判）')
    item.add_argument('--dry-run', action='store_true', help='只报告，不写库')
    item.add_argument('--limit', type=int, default=None, help='最多处理多少行（默认全部）')
    item.add_argument('--close-stale', action='store_true',
                      help='顺手关掉「来源已处理完、待办还挂着」的僵尸事件（默认不动）')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        raise SystemExit('--limit 必须是正整数')

    from . import reverify as reverify_tool
    from .transport import Transport

    database = Path(home) / 'state.sqlite3'
    if not database.exists():
        raise SystemExit('未找到状态库 %s；请先运行 install.py 并启动 feishu-bridge 服务' % database)
    config = _load_config(home)
    open_id = config.get('target_open_id')
    if not isinstance(open_id, str) or not open_id.startswith('ou_'):
        raise SystemExit('config.json 里的 target_open_id 不是用户 open_id：无法核对回读的发送者')
    store = Store(str(database))
    try:
        results = reverify_tool.reverify(store, Transport(config, threading.Event()), open_id,
                                         dry_run=args.dry_run, limit=args.limit)
        # 修好一行时它的配套待办已在同一事务里关掉；这里清的是历史上已经修好的那批。
        stale = (reverify_tool.close_stale(store, dry_run=args.dry_run)
                 if args.close_stale else None)
    finally:
        store.close()
    print(reverify_tool.format_table(results))
    if stale is not None:
        print(reverify_tool.format_stale(stale, dry_run=args.dry_run))
    if args.dry_run:
        print('（--dry-run：以上只是重判结果，没有写库）')
    return 0


def _attention(home, argv):
    """待人工项的正经出口：列表 + 关单（必须留理由，且不伪造回复）。

    以前这两件事只能手改库：``needs_attention`` 的消息和 ``pending/dead`` 的待办事件没有
    关单路径，账本上会一直挂着已经不需要处理的东西。
    """
    from . import attention as attention_tool

    parser = argparse.ArgumentParser(
        prog='feishu-bridge attention', description='待人工项：列出来、按 id 关单')
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('list', help='列出所有待人工项（带可粘贴的 ref）')
    closing = actions.add_parser(
        'close', help='按 ref 关单（inbox:<消息 id> / outbox:<行号> / event:<事件 id>）')
    closing.add_argument('ref', help='attention list 里第一列那串')
    closing.add_argument('--note', required=True, help='为什么关（必填，留档）')
    args = parser.parse_args(argv)

    database = Path(home) / 'state.sqlite3'
    if not database.exists():
        raise SystemExit('未找到状态库 %s；请先运行 install.py' % database)
    store = Store(str(database))
    try:
        if args.action == 'list':
            rows = attention_tool.items(store)
            if not rows:
                print('没有待人工项')
                return 0
            print(attention_tool.format_table(rows))
            return 0
        note = (args.note or '').strip()
        if not note:
            print('关单必须留理由：--note 不能为空（以后看账本要能分清为什么关的）')
            return 2
        result = attention_tool.close(store, args.ref, note, time.time())
        print(attention_tool.format_close(result))
        return 0 if result.get('ok') else 1
    finally:
        store.close()


def _notify(home, argv):
    """给本人发一条私聊（求助 / 需要拍板 / 漏答提醒）。

    与 ``events reply`` 的区别：这不是"对某条消息的回复"，没有原消息可回，所以不走本地
    outbox；与 ``self-chat`` 回执的区别：收件人是本人的私聊（``target_open_id``），
    不是某个群或自聊。

    三条闸门，缺一条就会变成刷屏：正文只从 stdin/文件读（argv 会进 ps）、按去重键合并
    重复通知、每小时上限（``bridge/notify.py``）。发送失败不改状态，下一次还能再试。
    """
    parser = argparse.ArgumentParser(
        prog='feishu-bridge notify',
        description='给本人发私聊（Agent 求助/需要本人拍板/答不上来的提醒）；同一件事 6 小时内只发一次')
    parser.add_argument('--body-file', default=None, help='正文文件；默认从 stdin 读（不接受 argv 明文）')
    parser.add_argument('--dedupe-key', default=None, help='去重键（默认按最终正文哈希）')
    parser.add_argument('--dry-run', action='store_true', help='只做参数与去重检查，不发送')
    parser.add_argument('--question-event', type=int, default=None,
                        help='这条私聊是某个事件（message/attention）的求助：登记待答，本人引用回复即可作答')
    parser.add_argument('--needs', default=None,
                        help='求助类型：fact/decision/access/ambiguity/uncertainty（只记录，用于回执措辞）')
    args = parser.parse_args(argv)
    config = _load_config(home)
    body = _read_reply_body(args.body_file, 'notify')
    result = _send_owner_notice(home, config, body, args.dedupe_key,
                                question_event=args.question_event, needs=args.needs,
                                dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 2


def _events_parser():
    parser = argparse.ArgumentParser(prog='feishu-bridge events',
                                     description='通用事件订阅通道（竞争消费，至少一次 + 按 event_id 幂等）')
    parser.add_argument('--home', default=None, help='状态目录（默认与 bridge 相同；也可写在 events 之前）')
    commands = parser.add_subparsers(dest='events_command', required=True)
    register = commands.add_parser('register', help='注册消费者（幂等，重复注册只更新 kind/last_seen）')
    register.add_argument('--consumer', required=True)
    register.add_argument('--kind', default='other', choices=('dsh', 'codex', 'other'))
    register.add_argument('--role', default='replier', choices=Store.CONSUMER_ROLES,
                          help='replier=回话端（领 message/attention，默认）；'
                               'worker=干活端（领 work）')
    listing = commands.add_parser('list', help='只读查看事件（默认最新 50 条）')
    listing.add_argument('--consumer')
    listing.add_argument('--state', help='逗号分隔：pending,leased,done,dead')
    listing.add_argument('--limit', type=int, default=50)
    claim = commands.add_parser('claim', help='领取事件（租约内独占；空结果表示无事件，退避后重试）')
    claim.add_argument('--consumer', required=True)
    claim.add_argument('--limit', type=int, default=5)
    claim.add_argument('--lease', type=int, default=300,
                       help='租约秒数，默认 300（LLM 处理慢时用 600）')
    claim.add_argument('--kind', choices=Store.EVENT_KINDS,
                       help='只领这一种（要与本消费者的 role 匹配）')
    ack = commands.add_parser('ack', help='确认事件已处理完成')
    ack.add_argument('--consumer', required=True)
    ack.add_argument('--event', type=int, required=True)
    ack.add_argument('--claim-token', required=True, help='本次 claim 返回的令牌，重领后旧令牌失效')
    fail = commands.add_parser('fail', help='报告处理失败（默认延迟 30s 重试；--permanent 直接进死信）')
    fail.add_argument('--consumer', required=True)
    fail.add_argument('--event', type=int, required=True)
    fail.add_argument('--claim-token', required=True)
    fail.add_argument('--error', default='External consumer failed')
    fail.add_argument('--retry-delay', type=int, default=30)
    fail.add_argument('--permanent', action='store_true')
    reply = commands.add_parser('reply', help='提交回复正文（只从 stdin 或 --body-file 读，禁止 argv，防 ps 泄露）')
    reply.add_argument('--consumer', required=True)
    reply.add_argument('--event', type=int, required=True)
    reply.add_argument('--claim-token', required=True)
    reply.add_argument('--body-file', default='-', help='默认 - 表示 stdin；也可给文件路径')
    reply.add_argument('--receipt-file', default=None,
                       help='回执文件：这条消息实际做了什么（记在哪、撤了哪条、提醒设在几点）。'
                            '和正文一样只从文件读，避免 ps 泄露；bridge 把它拼在署名之后——'
                            '落款在前、附注在后，顺序只在这里说了算')
    reply.add_argument('--task-id', default=None, help='把这步记到哪个任务（默认按消息自动归属）')
    reply.add_argument('--task-state', default=None,
                       choices=['open', 'running', 'blocked', 'done', 'failed', 'cancelled'],
                       help='这一轮之后任务处于什么状态；不给就按"回复发出=做完了"记 done')
    reply.add_argument('--task-note', default=None, help='这一步做到了什么（≤500 字）')
    reply.add_argument('--task-question', default=None,
                       help='state=blocked 时要问本人的一句话（≤300 字）')
    reply.add_argument('--task-artifacts-json', default=None,
                       help='这一轮的产物清单 JSON：[{"path": "...", "kind": "file"}]')
    skip = commands.add_parser('skip', help='判定这条消息不用回：事件收尾、不发任何消息（正文一个都不发）')
    skip.add_argument('--consumer', required=True)
    skip.add_argument('--event', type=int, required=True)
    skip.add_argument('--claim-token', required=True)
    skip.add_argument('--reason', required=True, help='为什么不回（一句话，记进台账；不发给任何人）')
    say = commands.add_parser('say', help='补一句说明：写进 outbox 由 sender 发，不结束任务、不改消息状态')
    say.add_argument('--consumer', required=True)
    say.add_argument('--event', type=int, default=None,
                     help='事件内 say：哪条事件触发的（必须与 --claim-token 一起给）')
    say.add_argument('--claim-token', default=None)
    say.add_argument('--chat', default=None,
                     help='主动消息：直接发到这个会话（与 --event/--reply-to 互斥）')
    say.add_argument('--reply-to', default=None,
                     help='挂到这条已有消息下面补一句（与 --chat/--event 互斥）')
    say.add_argument('--uuid', default=None,
                     help='幂等键：重放同一条命令不会多发一句（不给就按来源+正文派生）')
    say.add_argument('--body-file', default='-', help='默认 - 表示 stdin；也可给文件路径')
    retract = commands.add_parser('retract',
                                  help='撤回本桥发出去的一条消息（只撤自己发的；拒绝执行时退出码 4）')
    retract.add_argument('--consumer', required=True)
    retract.add_argument('--event', type=int, required=True)
    retract.add_argument('--claim-token', required=True)
    retract.add_argument('--message-id', required=True,
                         help='要撤回的那条飞书消息 id（必须是本桥发出去的）')
    retract.add_argument('--uuid', default=None, help='幂等键：重放同一条命令不会多排一次撤回')
    prune = commands.add_parser('prune', help='清理过期消息与死信（死信事件删除、陈旧未完成工作作废）')
    prune.add_argument('--older-than-days', type=float, default=7.0,
                       help='只处理早于 N 天的记录，默认 7')
    prune.add_argument('--include-done', action='store_true',
                       help='连已完成的旧事件一起删（默认保留，用于幂等去重）')
    prune.add_argument('--dry-run', action='store_true', help='只统计不修改')
    commands.add_parser('stats', help='事件队列统计')
    timeout = commands.add_parser('timeout', help='设置外部消费者回复超时（秒）；超时未回记为放弃')
    timeout.add_argument('seconds', type=int)
    commands.add_parser('status', help='事件队列 + 消费者 + 回复超时摘要')
    token = commands.add_parser('token', help='生成/查看 HTTP 网关共用 token（0600 文件；远程消费者用）')
    token.add_argument('--rotate', action='store_true', help='轮换 token（旧 token 在重启 events serve 后才失效；重启后同步更新远程消费者）')
    token.add_argument('--token-file', default=None, help='默认 HOME/gateway-token')
    serve = commands.add_parser('serve', help='启动 HTTP 事件网关（默认只绑 127.0.0.1:8766），供 Windows 等远程消费者调用')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--token-file', default=None)
    serve.add_argument('--allow-remote', action='store_true',
                       help='允许绑定非回环地址；请配合防火墙或 SSH 隧道，不要直接暴露公网')
    return parser


# --- external switch (home/external.json, atomic write) ---

EXTERNAL_DEFAULT_TIMEOUT = 600
EXTERNAL_TIMEOUT_RANGE = (60, 86400)


def _external_config(home):
    """external.json 现在只有一个事实：外部回复超时。开关（enabled）已删除，不再读它。"""
    data = _read_json(Path(home) / 'external.json')
    timeout = data.get('reply_timeout_seconds')
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        timeout = EXTERNAL_DEFAULT_TIMEOUT
    return {'reply_timeout_seconds': timeout}


def _set_external_timeout(home, args):
    """external.json 只剩一个可调项：回复超时。开关本身已删除（消费者是唯一大脑）。"""
    home = Path(home)
    low, high = EXTERNAL_TIMEOUT_RANGE
    if not low <= args.seconds <= high:
        raise SystemExit('超时必须在 %s..%s 秒之间' % (low, high))
    path = home / 'external.json'
    state = {'reply_timeout_seconds': int(args.seconds)}
    _atomic_json(path, state)
    print(json.dumps(dict(state, path=str(path),
                          note='已更新：service 按 mtime 读取，无需重启'),
                     ensure_ascii=False, indent=2))


# --- event view helpers ---

def _event_by_id(store, event_id):
    return store.get_event(event_id)


# --- events subcommands (JSON on stdout; failures exit non-zero) ---

def _events_register(store, args):
    now = time.time()
    existing = {consumer['name'] for consumer in store.consumers()}
    consumer = store.register_consumer(args.consumer, args.kind, now, role=args.role)
    print(json.dumps({'ok': True, 'created': consumer['name'] not in existing, 'consumer': consumer},
                     ensure_ascii=False, indent=2))


def _events_list(store, args):
    if not 1 <= args.limit <= 200:
        raise ValueError('--limit 必须在 1..200')
    states = None
    if args.state:
        states = [state.strip() for state in args.state.split(',') if state.strip()]
        unknown = [state for state in states if state not in Store.EVENT_STATES]
        if unknown:
            raise ValueError('未知事件状态：%s（可用：%s）' % (','.join(unknown), ','.join(Store.EVENT_STATES)))
    events = store.list_events(consumer=args.consumer or None, states=states or None, limit=args.limit)
    print(json.dumps({'count': len(events),
                      'events': [event_view(event, detailed=True, with_token=True) for event in events]},
                     ensure_ascii=False, indent=2))


def message_text_of(event):
    """事件里那条消息的正文；取不到就空串（检索不到资料，准则照给）。"""
    payload = parse_payload(event.get('payload'))
    if isinstance(payload, dict):
        text = payload.get('text')
        if isinstance(text, str):
            return text
        owner_answer = payload.get('owner_answer')
        if isinstance(owner_answer, str):
            return owner_answer
    return ''


def _events_claim(store, args, home):
    if not 1 <= args.limit <= 50:
        raise ValueError('--limit 必须在 1..50')
    if args.lease <= 0:
        raise ValueError('--lease 必须为正整数（秒）')
    now = time.time()
    claimed = store.claim_events(args.consumer, now, limit=args.limit,
                                 lease_seconds=args.lease, kind=args.kind)
    # 每条事件按**自己的正文**检索教材：不同消息需要的资料不一样，共用一块全量注入
    # 既浪费预算又会用不相关的条目干扰判断（L2-a，见 CONTRACTS「记忆检索」）。
    views = []
    for event in claimed:
        block = memory.retrieve(home, message_text_of(event))
        if block.get('enabled'):
            store.set_event_used_ids(event['id'], block.get('used_ids') or [])
        views.append(event_view(event, with_token=True, teachings=block))
    events = views
    print(json.dumps({'consumer': args.consumer, 'now': now, 'count': len(events), 'events': events},
                     ensure_ascii=False, indent=2))


def _events_ack(store, args):
    ok = store.ack_event(args.consumer, args.event, time.time(), claim_token=args.claim_token)
    print(json.dumps({'ok': ok, 'consumer': args.consumer, 'event': args.event}, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit('ack 未生效：事件 %s 未由消费者 %s 持有有效令牌和租约'
                         '（message 事件没有 ack 这条路：它的终态只能由"真的回了"达成；'
                         'attention 与 work 可以 ack）' % (args.event, args.consumer))


def _events_fail(store, args):
    if args.retry_delay < 0:
        raise ValueError('--retry-delay 不能为负（秒）')
    state = store.fail_event(args.consumer, args.event, time.time(), args.error,
                             retry_delay=args.retry_delay, permanent=args.permanent,
                             claim_token=args.claim_token)
    if not state:
        raise SystemExit('fail 未生效：事件 %s 未由消费者 %s 持有有效租约（可能已过期或已被接管）'
                         % (args.event, args.consumer))
    print(json.dumps({'ok': True, 'consumer': args.consumer, 'event': args.event, 'state': state},
                     ensure_ascii=False, indent=2))


def _read_reply_body(body_file, what='reply'):
    """正文只从 stdin 或文件读：argv 会被 ps 看见，禁止明文传正文。

    ``reply`` 与 ``notify`` 共用这一条路：两条出口都会把内容送到飞书，参数化 ``what``
    只是为了错误信息说对人话。
    """
    if body_file in (None, ''):
        body_file = '-'
    if body_file == '-':
        if sys.stdin is None or sys.stdin.isatty():
            raise SystemExit('%s 正文必须通过 stdin 提供（管道/here-doc），或用 --body-file PATH；'
                             '不接受 argv 明文，避免 ps 泄露' % what)
        try:
            text = sys.stdin.read()
        except (OSError, UnicodeError) as error:
            raise SystemExit('无法读取 stdin 正文：%s' % error)
    else:
        try:
            text = Path(body_file).read_text()
        except (OSError, UnicodeError) as error:
            raise SystemExit('无法读取 --body-file %s：%s' % (body_file, error))
    if not isinstance(text, str) or not text.strip():
        raise SystemExit('%s 正文为空：请通过 stdin 或 --body-file 提供正文' % what)
    return text


def _read_maybe_empty(body_file, what='approve'):
    """读正文但允许为空：批准时的"空答复"是一个**合法状态**（追问），不是用法错误。

    ``_read_reply_body`` 拒绝空正文是对的（发出去的空回复没有意义），但批准要能分清
    "他什么也没说"（追问一次）与"他说了不做"（拒绝）——两者都不是错误。
    """
    if body_file in (None, ''):
        body_file = '-'
    if body_file == '-':
        if sys.stdin is None or sys.stdin.isatty():
            raise SystemExit('%s 正文必须通过 stdin 提供（管道/here-doc），或用 --body-file PATH' % what)
        try:
            return sys.stdin.read()
        except (OSError, UnicodeError) as error:
            raise SystemExit('无法读取 stdin 正文：%s' % error)
    try:
        return Path(body_file).read_text()
    except (OSError, UnicodeError) as error:
        raise SystemExit('无法读取 --body-file %s：%s' % (body_file, error))


def _task_update_from_args(args):
    """把 reply 上的 --task-* 组装成账本更新；一个都没给就返回 None（老消费者行为不变）。"""
    update = {}
    if getattr(args, 'task_id', None):
        update['id'] = args.task_id
    if getattr(args, 'task_state', None):
        update['state'] = args.task_state
    if getattr(args, 'task_note', None):
        update['note'] = args.task_note
    if getattr(args, 'task_question', None):
        update['open_question'] = args.task_question
    raw = getattr(args, 'task_artifacts_json', None)
    if raw:
        try:
            artifacts = json.loads(raw)
        except ValueError:
            raise SystemExit('--task-artifacts-json 不是合法 JSON')
        if not isinstance(artifacts, list):
            raise SystemExit('--task-artifacts-json 必须是数组')
        update['artifacts'] = artifacts
    return update or None


def _events_reply(store, args):
    from .reply import owner_name_at, render_reply
    if hold_switch.is_active(Path(args.bridge_home)):
        raise SystemExit('bridge 处于挂起状态（mode=held）：此时不发送任何回复，'
                         '事件 %s 请保持未 ack（恢复后再 reply），或用 fail 退回队列' % args.event)
    # 先校验账本参数再读正文：坏参数不该先把 stdin 吃掉（消费者拿不到第二次机会）。
    task_update = _task_update_from_args(args)
    text = _read_reply_body(args.body_file)
    event = _event_by_id(store, args.event)
    if event is None:
        raise SystemExit('事件 %s 不存在' % args.event)
    if event['kind'] != 'message':
        raise SystemExit('事件 %s 是 %s 类型，不能 reply（只有 message 事件可回复）' % (args.event, event['kind']))
    # Store atomically validates claims and accepts matching retries of completed events.
    payload = parse_payload(event['payload'])
    mid = payload.get('message_id') if isinstance(payload, dict) else None
    if not isinstance(mid, str) or not mid:
        raise SystemExit('事件 %s 的 payload 缺少 message_id，无法回复' % args.event)
    config = _read_json(Path(args.bridge_home) / 'config.json')
    receipt = None
    if getattr(args, 'receipt_file', None):
        receipt = _read_reply_body(args.receipt_file, 'reply')
    # 群里回复 @ 提问的人（不是 @ 本人）；私聊不加 @。身份 id 都来自这一条事件的事实。
    chat_type = payload.get('chat_type') if isinstance(payload, dict) else None
    mention = payload.get('sender_id') if chat_type == 'group' and isinstance(payload, dict) else None
    body = render_reply(text, config.get('signature'), owner=owner_name_at(args.bridge_home),
                        receipt=receipt, mention=mention,
                        self_open_id=config.get('target_open_id'))
    result = store.complete_external_reply(args.consumer, args.event, time.time(), body, reply_uuid(mid),
                                           claim_token=args.claim_token,
                                           app_daily_cap=config.get('app_daily_reply_cap', 20),
                                           task=task_update)
    if not result.get('ok'):
        raise SystemExit('reply 被拒绝：%s' % result.get('reason', 'unknown reason'))
    print(json.dumps({'ok': True, 'duplicate': bool(result.get('duplicate')),
                      'consumer': args.consumer, 'event': args.event, 'message_id': mid,
                      'outbox_state': result.get('outbox_state'), 'body_length': len(body)},
                     ensure_ascii=False, indent=2))


def _events_skip(store, args):
    """「这条不用回」的收尾：事件 done、消息记 ignored、不发任何消息。"""
    reason = (args.reason or '').strip()
    if not reason:
        raise SystemExit('--reason 不能为空：为什么不回要留一句，方便以后复盘')
    result = store.complete_external_skip(args.consumer, args.event, time.time(), reason,
                                          claim_token=args.claim_token)
    if not result.get('ok'):
        raise SystemExit('skip 被拒绝：%s' % result.get('reason', 'unknown reason'))
    print(json.dumps({'ok': True, 'duplicate': bool(result.get('duplicate')),
                      'consumer': args.consumer, 'event': args.event,
                      'message_id': result.get('message_id'), 'reason': reason},
                     ensure_ascii=False, indent=2))


def _outbox_uuid(scope):
    """没有 ``--uuid`` 时的默认幂等键：同一来源 + 同一内容重放不会多写一行。"""
    return hashlib.sha256(('bridge-outbox:' + scope).encode('utf-8')).hexdigest()[:40]


def _events_say(store, args):
    """补一句说明：事件内（``--event`` + ``--claim-token``）、主动消息（``--chat``）、
    挂在已有消息下（``--reply-to``）。

    与 reply 的差别只有两条：不结束任务、不改 inbox 状态；正文照样过 ``render_reply``
    （署名/脱敏/去自 @），所以群里看到的声音和终回复是同一个。
    """
    from .reply import owner_name_at, render_reply
    if hold_switch.is_active(Path(args.bridge_home)):
        raise SystemExit('bridge 处于挂起状态（mode=held）：此时不发送任何消息；'
                         '恢复后再 say，或把这条事件留到恢复后处理')
    event_id, claim_token = args.event, args.claim_token
    if (event_id is None) != (not claim_token):
        raise SystemExit('--event 与 --claim-token 必须一起给：事件内 say 用的是这次 claim 的令牌')
    if event_id is None and not (args.chat or args.reply_to):
        raise SystemExit('say 需要三种来源之一：--event + --claim-token（事件内）、'
                         '--chat（主动消息）、--reply-to（挂在已有消息下）')
    if event_id is not None and (args.chat or args.reply_to):
        raise SystemExit('--event 与 --chat/--reply-to 互斥：事件内 say 的会话与原消息都由那条事件决定')
    if args.chat and args.reply_to:
        raise SystemExit('--chat 与 --reply-to 互斥：主动消息没有原消息，补话有原消息就不必再指定会话')
    text = _read_reply_body(args.body_file, 'say')
    config = _read_json(Path(args.bridge_home) / 'config.json')
    body = render_reply(text, config.get('signature'), owner=owner_name_at(args.bridge_home),
                        self_open_id=config.get('target_open_id'))
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    if event_id is not None:
        event = _event_by_id(store, event_id)
        if event is None:
            raise SystemExit('事件 %s 不存在' % event_id)
        if event['kind'] != 'message':
            raise SystemExit('事件 %s 是 %s 类型，不能 say（只有 message 事件有会话）'
                             % (event_id, event['kind']))
        payload = parse_payload(event['payload'])
        mid = payload.get('message_id') if isinstance(payload, dict) else None
        uid = args.uuid or _outbox_uuid('say:%s:%s' % (mid, digest))
        result = store.complete_external_reply(
            args.consumer, event_id, time.time(), body, uid, claim_token=claim_token,
            app_daily_cap=config.get('app_daily_reply_cap', 20), kind='say')
    else:
        scope = args.reply_to or args.chat
        uid = args.uuid or _outbox_uuid('say:%s:%s' % (scope, digest))
        result = store.publish_say(args.consumer, time.time(), body, uid,
                                   chat_id=args.chat, inbox_id=args.reply_to)
    if not result.get('ok'):
        reason = result.get('reason', 'unknown reason')
        if result.get('code') in ('inbox_not_found', 'chat_required'):
            sys.stderr.write('say 被拒绝：%s\n' % reason)
            raise SystemExit(4)
        raise SystemExit('say 被拒绝：%s' % reason)
    print(json.dumps({'ok': True, 'duplicate': bool(result.get('duplicate')),
                      'consumer': args.consumer, 'kind': 'say', 'event': event_id,
                      'inbox_id': result.get('inbox_id'), 'chat_id': result.get('chat_id'),
                      'row_id': result.get('row_id'), 'seq': result.get('seq'),
                      'outbox_state': result.get('outbox_state'), 'body_length': len(body)},
                     ensure_ascii=False, indent=2))


def _events_retract(store, args):
    """撤回本桥发出去的一条消息：只落一行 retract，真正的 DELETE 由 sender 执行。"""
    if hold_switch.is_active(Path(args.bridge_home)):
        raise SystemExit('bridge 处于挂起状态（mode=held）：此时不发送任何消息；恢复后再 retract')
    target = (args.message_id or '').strip()
    if not target:
        raise SystemExit('--message-id 不能为空')
    uid = args.uuid or _outbox_uuid('retract:%s' % target)
    result = store.complete_external_reply(args.consumer, args.event, time.time(), '', uid,
                                           claim_token=args.claim_token, kind='retract',
                                           target_mid=target)
    if not result.get('ok'):
        reason = result.get('reason', 'unknown reason')
        if result.get('code') == 'retract_target_not_found':
            sys.stderr.write('retract 被拒绝：%s\n' % reason)
            raise SystemExit(4)
        raise SystemExit('retract 被拒绝：%s' % reason)
    print(json.dumps({'ok': True, 'duplicate': bool(result.get('duplicate')),
                      'consumer': args.consumer, 'kind': 'retract', 'event': args.event,
                      'inbox_id': result.get('inbox_id'), 'row_id': result.get('row_id'),
                      'seq': result.get('seq'), 'outbox_state': result.get('outbox_state'),
                      'target_message_id': target}, ensure_ascii=False, indent=2))


def _events_token(home, args):
    from . import gateway
    path = Path(args.token_file).expanduser() if args.token_file else gateway.token_path(home)
    if path.exists() and not args.rotate:
        token, created = gateway.read_token(path), False
    else:
        token, path, created = gateway.generate_token(path.parent, rotate=True, path=path)
    print(json.dumps({'ok': True, 'created': created, 'token': token, 'path': str(path),
                      'note': '把 token 配到远程消费者（Authorization: Bearer <token>）；'
                              '轮换后旧 token 在重启 events serve 后才失效，重启后再更新远程消费者'}, ensure_ascii=False, indent=2))


def _events_serve(home, args):
    from . import gateway
    if not 1 <= args.port <= 65535:
        raise SystemExit('--port 必须在 1..65535')
    token_file = Path(args.token_file).expanduser() if args.token_file else gateway.token_path(home)
    try:
        token = gateway.read_token(token_file)
    except RuntimeError as error:
        raise SystemExit('%s；先运行 feishu-bridge events token 生成 token' % error)
    if args.host not in gateway.LOOPBACK_HOSTS and not args.allow_remote:
        raise SystemExit('拒绝绑定非回环地址 %s：如确需局域网访问请加 --allow-remote，'
                         '并确保防火墙/SSH 隧道安全（推荐 Windows 走 ssh -L 转发到 127.0.0.1）' % args.host)
    database = Path(home) / 'state.sqlite3'
    if not database.exists():
        raise SystemExit('未找到状态库 %s；请先运行 install.py 并启动 feishu-bridge 服务' % database)
    try:
        store = Store(str(database))
    except RuntimeError as error:
        raise SystemExit('状态库版本不兼容：%s' % error)
    except (OSError, sqlite3.Error) as error:
        raise SystemExit('无法打开状态库 %s：%s' % (database, error))
    address = 'http://%s:%s' % (args.host, args.port)
    print(json.dumps({'ok': True, 'listening': address, 'token_file': str(token_file),
                      'endpoints': ['GET /health', 'GET /v1/events/status', 'GET /v1/events/{id}',
                                    'GET /v1/events?state=pending', 'POST /v1/events/claim',
                                    'POST /v1/events/{id}/ack|fail|reply|say|retract'],
                      'note': 'Ctrl-C 停止；token 用 Authorization: Bearer <token>'},
                     ensure_ascii=False, indent=2), flush=True)
    try:
        gateway.serve(gateway.Gateway(store, home, token), args.host, args.port)
    except OSError as error:
        raise SystemExit('网关启动失败（%s:%s）：%s' % (args.host, args.port, error))
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


def _events_prune(store, args):
    seconds = args.older_than_days * 86400
    if seconds < 0:
        raise SystemExit('--older-than-days 不能为负')
    report = store.prune_expired(time.time(), older_than_s=seconds,
                                 include_done=args.include_done, dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _events_stats(store, args):
    print(json.dumps(store.event_stats(time.time()), ensure_ascii=False, indent=2))


def _events_status(home):
    home = Path(home)
    database = home / 'state.sqlite3'
    # 顶层固定 {external, events, consumers}，agent 可直接解析；其余键是补充诊断信息。
    report = {'external': _external_config(home), 'events': {}, 'consumers': [],
              'path': str(home / 'external.json'), 'database': str(database),
              'db_present': database.exists()}
    if database.exists():
        try:
            store = Store(str(database))
        except (RuntimeError, OSError, sqlite3.Error) as error:
            report['error'] = '状态库无法打开：%s' % error
        else:
            try:
                report['events'] = store.event_stats(time.time())
                report['consumers'] = store.consumers()
            finally:
                store.close()
    else:
        report['error'] = '状态库不存在：%s；请先运行 install.py 并启动 feishu-bridge 服务' % database
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _run_events(home, argv):
    args = _events_parser().parse_args(argv)
    if args.home:
        home = Path(args.home).expanduser().resolve()
    if args.events_command == 'timeout':
        return _set_external_timeout(home, args)
    if args.events_command == 'status':
        return _events_status(home)
    if args.events_command == 'token':
        return _events_token(home, args)
    if args.events_command == 'serve':
        return _events_serve(home, args)
    home = Path(home)
    args.bridge_home = str(home)
    database = home / 'state.sqlite3'
    if not database.exists():
        raise SystemExit('未找到状态库 %s；请先运行 install.py 并启动 feishu-bridge 服务（events 复用服务状态库）'
                         % database)
    try:
        store = Store(str(database))
    except RuntimeError as error:
        raise SystemExit('状态库版本不兼容：%s' % error)
    except (OSError, sqlite3.Error) as error:
        raise SystemExit('无法打开状态库 %s：%s' % (database, error))
    handlers = {'register': _events_register, 'list': _events_list,
                'claim': lambda event_store, event_args: _events_claim(event_store, event_args, home),
                'ack': _events_ack, 'fail': _events_fail, 'reply': _events_reply,
                'skip': _events_skip, 'say': _events_say, 'retract': _events_retract,
                'stats': _events_stats, 'prune': _events_prune}
    try:
        handlers[args.events_command](store, args)
    except LookupError as error:
        raise SystemExit('消费者未注册：%s；请先运行 feishu-bridge events register --consumer %s --kind dsh'
                         % (error, getattr(args, 'consumer', '<name>')))
    except ValueError as error:
        raise SystemExit('events %s 失败：%s' % (args.events_command, error))
    except sqlite3.Error as error:
        raise SystemExit('状态库访问失败：%s' % error)
    finally:
        store.close()


def _command_index(argv):
    """第一个非选项 token 的下标（跳过 --home 的值），用于把 `events ...` 交给子解析器。

    `parse_known_args` 会把 `events --help` 的 --help 交给顶层解析器（打印顶层帮助），
    因此必须在 argparse 之前定位命令 token；`--home` 可以写在 `events` 之前。
    """
    skip_value = False
    for index, token in enumerate(argv):
        if skip_value:
            skip_value = False
            continue
        if token == '--home':
            skip_value = True
            continue
        if token.startswith('--home=') or token.startswith('-'):
            continue
        return index
    return None


def main(default_home=None):
    parser = argparse.ArgumentParser(description='飞书个人助手：用你自己的飞书账号收发消息')
    parser.add_argument('--home', default=default_home or str(host.default_home()))
    parser.add_argument('command', choices=['foreground', 'start', 'stop', 'restart', 'status', 'pause', 'resume',
                                            'hold', 'release',
                                            'logs', 'doctor', 'events', 'consumer', 'self-chat', 'bots', 'notify',
                                            'rollback', 'teach', 'help', 'commands', 'task', 'turns', 'latency',
                                            'remind', 'outbox', 'attention'])
    argv = list(sys.argv[1:])
    index = _command_index(argv)
    if index is not None and argv[index] == 'consumer':
        args = parser.parse_args(argv[:index] + ['consumer'])
        from .consumerctl import main as consumer_main
        # 退出码要传出去：`consumer forget` 拒绝删除活跃行时返回 1（脚本里靠它分支）。
        raise SystemExit(consumer_main(Path(args.home).expanduser().resolve(), argv[index + 1:]) or 0)
    if index is not None and argv[index] == 'bots':
        args = parser.parse_args(argv[:index] + ['bots'])
        return _bots(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'self-chat':
        args = parser.parse_args(argv[:index] + ['self-chat'])
        _self_chat(Path(args.home).expanduser().resolve(), argv[index + 1:])
        return
    if index is not None and argv[index] == 'rollback':
        args = parser.parse_args(argv[:index] + ['rollback'])
        _rollback(Path(args.home).expanduser().resolve(), argv[index + 1:])
        return
    if index is not None and argv[index] == 'events':
        # `events` 之后全部交给 events 子解析器（含 --help/-h），旧命令行为保持不变。
        head, rest = argv[:index], argv[index + 1:]
        args = parser.parse_args(head + ['events'])
        home = Path(args.home).expanduser().resolve()
        _run_events(home, rest)
        return
    if index is not None and argv[index] == 'notify':
        args = parser.parse_args(argv[:index] + ['notify'])
        raise SystemExit(_notify(Path(args.home).expanduser().resolve(), argv[index + 1:]) or 0)
    if index is not None and argv[index] == 'commands':
        # 命令表本身也给人看：说明书是它的渲染结果，两者永远同一份（不另写一份）。
        args = parser.parse_args(argv[:index] + ['commands'])
        from . import commands as command_table
        print('\n'.join(command_table.table_lines()))
        return 0
    if index is not None and argv[index] == 'help':
        args = parser.parse_args(argv[:index] + ['help'])
        return _manual(argv[index + 1:])
    if index is not None and argv[index] == 'teach':
        args = parser.parse_args(argv[:index] + ['teach'])
        return _teach(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'remind':
        args = parser.parse_args(argv[:index] + ['remind'])
        return _remind(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'task':
        args = parser.parse_args(argv[:index] + ['task'])
        return _task(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'attention':
        args = parser.parse_args(argv[:index] + ['attention'])
        # 退出码要传出去：关单失败/缺理由时脚本靠它分支（同 consumer forget 的做法）。
        raise SystemExit(_attention(Path(args.home).expanduser().resolve(),
                                    argv[index + 1:]) or 0)
    if index is not None and argv[index] == 'outbox':
        args = parser.parse_args(argv[:index] + ['outbox'])
        return _outbox(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'turns':
        args = parser.parse_args(argv[:index] + ['turns'])
        return _turns(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'latency':
        args = parser.parse_args(argv[:index] + ['latency'])
        return _latency(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'logs':
        args = parser.parse_args(argv[:index] + ['logs'])
        return _logs(Path(args.home).expanduser().resolve(), argv[index + 1:])
    if index is not None and argv[index] == 'doctor':
        args = parser.parse_args(argv[:index] + ['doctor'])
        return _doctor(Path(args.home).expanduser().resolve(), host.service_path(LABEL),
                       argv[index + 1:])
    if index is not None and argv[index] in ('hold', 'release'):
        args = parser.parse_args(argv[:index] + [argv[index]])
        _hold(Path(args.home).expanduser().resolve(), argv[index], argv[index + 1:])
        return
    args = parser.parse_args()
    home = Path(args.home).expanduser().resolve()
    service = host.service_path(LABEL)
    if args.command == 'foreground':
        from .service import foreground
        foreground(home)
    elif args.command == 'status':
        _print_status(home)
    elif args.command in ('pause', 'resume'):
        _pause_resume(home, args.command)
    elif args.command == 'logs':
        _logs(home)
    elif args.command == 'doctor':
        _doctor(home, service)
    elif host.is_macos():
        domain = 'gui/%s' % os.getuid()
        target = domain + '/' + LABEL
        with _cli_lock(home):
            try:
                _lifecycle(args.command, home, domain, target, service)
            except (OSError, subprocess.SubprocessError) as error:
                raise SystemExit('launchctl 调用失败：%s: %s；请检查 launchd 状态后重试 feishu-bridge %s'
                                 % (type(error).__name__, error, args.command))
        print(json.dumps(launch_status(), ensure_ascii=False, indent=2))
    elif host.is_linux():
        with _cli_lock(home):
            _lifecycle_systemd(args.command, service)
        print(json.dumps(systemd_status(service.name), ensure_ascii=False, indent=2))
    else:
        host.require_supported()


if __name__ == '__main__':
    main()
