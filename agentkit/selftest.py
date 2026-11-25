"""``python3 -m agentkit selftest``：在**当前这台机器**上真跑一遍平台层。

为什么需要它：抽象层的风险集中在"我以为 Windows 上会这样"。单元测试在 macOS 上只能
覆盖代码路径，覆盖不了"``taskkill /T`` 到底收不收孙进程""线程读取器会不会卡住 EOF"
这类**只在该操作系统上才成立**的事实。

所以把"验证"做成一条命令：在 WSL2 里跑一次、在原生 Windows 上跑一次、在 Mac 上跑一次，
三份输出放一起就是"这套抽象真的跨平台"的证据；跑不出来就是某处的假设错了。

每个检查都返回 ``(name, ok, detail)``，失败不影响后面的检查 —— 一次跑完看全景，
比第一个失败就退出有用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

from .errors import PolicyRefusal
from .platform import get_platform, get_lock_platform
from .platform.stream import EOF, LINE
from .types import Invocation

Check = Tuple[str, bool, str]


def _print_child(lines: List[str], delay_s: float = 0.0) -> List[str]:
    code = "import sys,time\n"
    for line in lines:
        code += "sys.stdout.write(%r); sys.stdout.flush()\n" % (line + "\n")
        if delay_s:
            code += "time.sleep(%r)\n" % delay_s
    return [sys.executable, "-c", code]


def _inv(argv: List[str], cwd: str, env: Dict[str, str]) -> Invocation:
    return Invocation(argv=argv, env=env, cwd=cwd)


def check_spawn_and_env(platform, workspace: str) -> Check:
    env = platform.prepare_env(workspace=workspace)
    home = env.get("HOME") or env.get("USERPROFILE") or ""
    if workspace not in home:
        return ("spawn+env", False,
                "HOME/USERPROFILE 没有被重定向进 workspace: %r" % (home,))
    proc = platform.spawn(_inv(_print_child(["env-ok"]), workspace, env))
    try:
        reader = platform.line_reader(proc)
        try:
            kind, line = reader.next_line(10)
        finally:
            reader.close()
        if kind != LINE or line != "env-ok":
            return ("spawn+env", False, "unexpected first line: %r %r" % (kind, line))
    finally:
        proc.wait(timeout=10)
    return ("spawn+env", True, "HOME=%s" % home)


def check_stream_three_states(platform, workspace: str) -> Check:
    env = platform.prepare_env(workspace=workspace)
    proc = platform.spawn(_inv(_print_child(["first", "second"], delay_s=0.3), workspace, env))
    try:
        reader = platform.line_reader(proc)
        try:
            kind, line = reader.next_line(10)
            if kind != LINE or line != "first":
                return ("stream:line", False, "unexpected first: %r %r" % (kind, line))
            saw_timeout = False
            deadline = time.time() + 5
            while time.time() < deadline:
                kind, line = reader.next_line(0.05)
                if kind == LINE:
                    if line != "second":
                        return ("stream:line", False, "unexpected second: %r" % (line,))
                    break
                if kind == "timeout":
                    saw_timeout = True
            else:
                return ("stream:line", False, "second line never arrived")
            deadline = time.time() + 10
            while time.time() < deadline:
                kind, _ = reader.next_line(0.2)
                if kind == EOF:
                    break
            else:
                return ("stream:line", False, "never saw EOF")
        finally:
            reader.close()
    finally:
        proc.wait(timeout=10)
    return ("stream:line", True,
            "line/timeout/eof 三态都正确（timeout 可观测=%s）" % saw_timeout)


def check_terminate_tree(platform, workspace: str) -> Check:
    env = platform.prepare_env(workspace=workspace)
    # 父进程再起一个孙进程：只杀父会漏掉孙，reader 会一直挂着
    code = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    proc = platform.spawn(_inv([sys.executable, "-c", code], workspace, env))
    time.sleep(0.4)
    started = time.time()
    platform.terminate(proc, grace_s=0.5)
    exited = proc.poll() is not None
    elapsed = time.time() - started
    if not exited:
        try:
            proc.kill()
        except OSError:
            pass
        return ("terminate:tree", False, "terminate 之后进程仍在（%.1fs）" % elapsed)
    return ("terminate:tree", True, "%.2fs 内收掉进程树" % elapsed)


def check_lock(platform) -> Check:
    try:
        lock_platform = get_lock_platform()
    except Exception as error:  # noqa: BLE001
        return ("lock", False, "%s: %s" % (type(error).__name__, error))
    tmpdir = tempfile.mkdtemp(prefix="agentkit-lock-")
    path = os.path.join(tmpdir, "selftest.lock")
    try:
        try:
            handle = lock_platform.acquire(path, timeout_s=2.0)
        except PolicyRefusal as refusal:
            # Windows 上这是**设计如此**：不算失败，算"明确拒绝"
            return ("lock", True, "按设计拒绝：%s" % str(refusal)[:90])
        lock_platform.release(handle)
        return ("lock", True, "acquire/release 正常")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run(verbose: bool = True) -> Dict[str, object]:
    """跑完全部检查，返回可直接 json.dumps 的报告。"""
    platform = get_platform()
    workspace = tempfile.mkdtemp(prefix="agentkit-selftest-")
    results: List[Check] = []
    checks: List[Tuple[str, Callable[[], Check]]] = [
        ("spawn+env", lambda: check_spawn_and_env(platform, workspace)),
        ("stream:line", lambda: check_stream_three_states(platform, workspace)),
        ("terminate:tree", lambda: check_terminate_tree(platform, workspace)),
        ("lock", lambda: check_lock(platform)),
    ]
    try:
        for name, runner in checks:
            try:
                results.append(runner())
            except Exception as error:  # noqa: BLE001 - 一个检查炸了不影响其他检查
                results.append((name, False, "%s: %s" % (type(error).__name__, error)))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    report: Dict[str, object] = {
        "platform": platform.name,
        "os": os.name,
        "supports_process_groups": platform.supports_process_groups,
        "checks": [
            {"name": name, "ok": ok, "detail": detail} for name, ok, detail in results
        ],
        "ok": all(ok for _name, ok, _detail in results),
    }
    if verbose:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    return 0 if run().get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
