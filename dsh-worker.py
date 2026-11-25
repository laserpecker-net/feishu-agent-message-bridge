#!/usr/bin/env python3
"""干活端进程：领 ``work`` 事件，在独立工作树里干，把结论记进账本。

**它不说话**：回群、发私聊是 PR-4 的事。这里只做四件事——注册（role=worker）、自检、
常驻领活、把结论落账。运行机制（overlay / NDJSON / 超时 / 进程树 / 交接单）在
``bridge/workbench.py``；领活与开工作树在 ``bridge/worker.py``。本文件只保留进程外壳：

  * 启动自检不过就**直接退出**并打印下一步（不进入"看起来在跑、其实干不了"的状态）；
  * 一个消费者名在一台机器上只能有一个进程（``flock``），否则两个进程会抢同一件活；
  * 收到 SIGTERM/SIGINT：先把当前这件活收尾（交接包），再退。
"""
import argparse
import fcntl
import os
import signal
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge.store import Store            # noqa: E402  (脚本直跑时没有包上下文)
from bridge import worker as worker_module  # noqa: E402
from bridge import workbench as workbench_module  # noqa: E402


def _default_home():
    return Path(os.environ.get('FEISHU_BRIDGE_HOME')
                or (Path.home() / 'Library/Application Support/FeishuPersonalAgent'))


def build_parser():
    parser = argparse.ArgumentParser(description='飞书 bridge 干活端（只领 work 事件）')
    parser.add_argument('--consumer', default=None, help='消费者名，默认 dsh-worker')
    parser.add_argument('--home', type=Path, default=_default_home())
    parser.add_argument('--max-concurrency', type=int, default=None, help='同时跑几件活，默认 1')
    parser.add_argument('--check', action='store_true', help='只做自检，不注册、不领活')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    home = Path(args.home).expanduser()
    config = worker_module.read_config(home)
    settings = worker_module.worker_config(config, home=home,
                                           consumer=args.consumer,
                                           max_concurrency=args.max_concurrency)
    store = Store(str(home / 'state.sqlite3'))
    worker = worker_module.Worker(store, settings, raw_config=config)
    if args.check:
        # 自检与真跑**必须用同一个实例**：`prepare()` 会把 cli_version 探针的结果记在这个对象上，
        # 另起一个对象再 describe，就会打印「cli_version 未知」（2026-11-20 真机踩到）。
        work_root = Path(settings.get('work_root') or (Path(settings['home']) / 'work'))
        probe = worker.workbench_factory(work_root)
        probe.prepare()
        print('干活端可用：consumer=%s 工作目录根=%s 并发=%d'
              % (settings['consumer'], work_root, settings['max_concurrency']))
        # 工作台自检：**两端打印同样的行**（launch/kind/cli/home/... ），只有值不同。
        # 这样"两端同一组判据"是能被对照的，不是各说各话。
        rows = probe.describe() if hasattr(probe, 'describe') else []
        width = max([len(name) for name, _value, _ok in rows] + [8])
        for name, value, ok in rows:
            print('  %-*s %-40s %s' % (width, name, value, 'PASS' if ok else 'FAIL'))
        if any(not ok for _n, _v, ok in rows):
            return 2
        return 0
    try:
        worker.register()   # role=worker；注册不上就别领活
        worker.prepare()    # 工作台起不来就立刻失败，别空转
    except Exception as error:  # noqa: BLE001 - 退出前给出"下一步"，不做静默降级
        print('干活端起不来：%s' % error, file=sys.stderr)
        print('下一步：检查 config.json 的 workbench 块（cli/home/workspace）',
              file=sys.stderr)
        return 2

    state = home / 'consumer-state' / settings['consumer']
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    lock_path = state / 'worker.lock'
    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('同一台机器上已经有干活端在跑了（锁：%s）' % lock_path, file=sys.stderr)
            return 2
        print('干活端已就绪：consumer=%s 工作目录根=%s'
              % (settings['consumer'], Path(settings['home']) / 'work'),
              flush=True)
        worker.run(stop)
    return 0


if __name__ == '__main__':
    sys.exit(main())
