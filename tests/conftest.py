"""全局判据：一个用例不许把线程留在后台继续跑。

为什么要有这条：共用数据库连接的年代，泄漏的 worker 线程会在下一个用例关库之后
继续执行 SQL，把 libsqlite3 的堆写坏，表现是**随机 SIGSEGV（rc=139）**，而且
崩的位置和真正的原因毫无关系。线程泄漏不是"测试洁癖"，它是那次事故的引信。
"""
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def no_leaked_threads():
    before = set(threading.enumerate())
    yield
    created = [t for t in threading.enumerate() if t not in before]
    deadline = time.monotonic() + 2.0
    for thread in created:
        thread.join(max(0.0, deadline - time.monotonic()))
    leaked = sorted(t.name for t in created if t.is_alive())
    assert not leaked, '用例结束时仍有线程活着：%s' % ', '.join(leaked)
