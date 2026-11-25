"""平台层测试：环境组装、流读取、进程终止、工厂策略门。

这些用例刻意不碰真实 AGENT —— 平台层的契约与 AGENT 无关，用当前解释器就能验完。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import time

import pytest

from agentkit import errors
from agentkit.platform import NATIVE_WINDOWS_FLAG, describe, get_platform
from agentkit.platform import posix as posix_platform
from agentkit.types import Invocation
from agentkit.platform import windows as windows_platform
from agentkit.platform.stream import EOF, LINE, TIMEOUT, ThreadLineReader


# ---------------------------------------------------------------------------
# 抽象工厂
# ---------------------------------------------------------------------------
def test_factory_returns_a_complete_platform_family():
    platform = get_platform()
    assert platform.name in ("posix", "windows")
    # 一套原语必须齐全：缺任何一个都说明"工厂没造完整"
    for attr in ("spawn", "terminate", "line_reader", "prepare_env", "which", "paths"):
        assert hasattr(platform, attr), attr
    assert describe()["platform"] == platform.name


def test_native_windows_is_refused_by_default(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv(NATIVE_WINDOWS_FLAG, raising=False)
    from agentkit import platform as platform_mod

    platform_mod._CACHE.clear()
    with pytest.raises(errors.UnsupportedPlatform) as excinfo:
        get_platform()
    assert "WINDOWS-DECISION.md" in str(excinfo.value)
    platform_mod._CACHE.clear()


def test_native_windows_needs_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv(NATIVE_WINDOWS_FLAG, "1")
    from agentkit import platform as platform_mod

    platform_mod._CACHE.clear()
    assert get_platform().name == "windows"
    platform_mod._CACHE.clear()


def test_windows_has_no_cross_process_lock_by_design():
    lock = windows_platform.WindowsLockPlatform()
    with pytest.raises(errors.PolicyRefusal):
        lock.acquire("whatever")


# ---------------------------------------------------------------------------
# 环境组装：本报告里三条"不看就踩"的实测结论
# ---------------------------------------------------------------------------
def test_home_tmpdir_and_pwd_are_redirected_into_the_workspace(tmp_path):
    platform = posix_platform.PosixProcessPlatform()
    workspace = str(tmp_path)
    env = platform.prepare_env(workspace=workspace)
    agentkit_home = os.path.join(workspace, ".agentkit", "home")
    assert env["HOME"] == agentkit_home
    assert env["TMPDIR"] == os.path.join(workspace, ".agentkit", "tmp")
    assert env["PWD"] == workspace
    # transcript 必须落在 workspace 内，否则回收/换机后续跑失忆
    assert env["HOME"].startswith(workspace)
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_secret_shaped_variables_never_reach_the_child(tmp_path):
    platform = posix_platform.PosixProcessPlatform()
    base = {
        "PATH": "/usr/bin",
        "DEEPSEEK_API_KEY": "sk-should-not-leak",
        "GITHUB_TOKEN": "ghp-should-not-leak",
        "SOME_PASSWORD": "hunter2",
        "ORDINARY_VALUE": "keep-me",
    }
    env = platform.prepare_env(base=base, workspace=str(tmp_path))
    assert "DEEPSEEK_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "SOME_PASSWORD" not in env
    assert env["ORDINARY_VALUE"] == "keep-me"
    # 显式 allowlist 时才放行（走 relay/占位符的 adapter 用得到）
    env2 = platform.prepare_env(
        base=base, workspace=str(tmp_path), allow_secrets=["DEEPSEEK_API_KEY"]
    )
    assert env2["DEEPSEEK_API_KEY"] == "sk-should-not-leak"
    assert "GITHUB_TOKEN" not in env2


def test_adapter_cannot_override_protected_variables(tmp_path):
    platform = posix_platform.PosixProcessPlatform()
    with pytest.raises(errors.PolicyRefusal):
        platform.prepare_env(workspace=str(tmp_path), extra={"HOME": "/etc"})
    with pytest.raises(errors.PolicyRefusal):
        platform.prepare_env(workspace=str(tmp_path), extra={"SOME_API_KEY": "x"})


def test_loader_injection_variables_are_dropped(tmp_path):
    platform = posix_platform.PosixProcessPlatform()
    env = platform.prepare_env(
        base={"LD_PRELOAD": "/tmp/evil.so", "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib"},
        workspace=str(tmp_path),
    )
    assert "LD_PRELOAD" not in env
    assert "DYLD_INSERT_LIBRARIES" not in env


# ---------------------------------------------------------------------------
# 流读取：POSIX selectors 版与线程版必须给出同样的三种结果
# ---------------------------------------------------------------------------
def _slow_printer() -> subprocess.Popen:
    script = (
        "import sys,time\n"
        "print('one', flush=True)\n"
        "time.sleep(0.4)\n"
        "print('two', flush=True)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


@pytest.mark.parametrize("factory", ["posix", "thread"])
def test_line_reader_reports_line_then_timeout_then_eof(factory):
    proc = _slow_printer()
    reader = posix_platform.PosixLineReader(proc) if factory == "posix" else ThreadLineReader(proc)
    try:
        kind, line = reader.next_line(5)
        assert (kind, line) == (LINE, "one")
        # 第二条还没到：必须报 TIMEOUT 而不是阻塞或 EOF
        kind, _ = reader.next_line(0.05)
        assert kind == TIMEOUT
        deadline = time.time() + 5
        while time.time() < deadline:
            kind, line = reader.next_line(0.2)
            if kind == LINE:
                assert line == "two"
                break
        else:  # pragma: no cover
            pytest.fail("never received the second line")
        deadline = time.time() + 5
        while time.time() < deadline:
            kind, _ = reader.next_line(0.2)
            if kind == EOF:
                break
        else:  # pragma: no cover
            pytest.fail("never saw EOF")
    finally:
        reader.close()
        proc.wait(timeout=5)


def test_platform_selftest_passes_on_this_machine():
    """``python3 -m agentkit selftest`` 是"在真机上验一遍"的入口，它本身也要被测。"""
    from agentkit import selftest

    report = selftest.run(verbose=False)
    assert report["ok"] is True, report
    names = {check["name"] for check in report["checks"]}
    assert {"spawn+env", "stream:line", "terminate:tree", "lock"} <= names


def test_inherit_home_keeps_the_real_home_but_still_redirects_temp(tmp_path):
    """inherit 是**窄化例外**：HOME 保留真实值（登录态在那里），临时目录仍然收进工作区。"""
    platform = posix_platform.PosixProcessPlatform()
    workspace = str(tmp_path)
    real_home = os.environ.get("HOME")
    env = platform.prepare_env(workspace=workspace, home_mode="inherit")
    assert env["HOME"] == real_home
    assert env["USERPROFILE"] == real_home
    assert env["TMPDIR"].startswith(workspace)      # 临时文件仍然不落宿主
    assert env["PWD"] == workspace
    # 对照：默认 redirect 必须把 HOME 收进 workspace
    redirected = platform.prepare_env(workspace=workspace)
    assert redirected["HOME"] != real_home
    assert redirected["HOME"].startswith(workspace)


def test_cursor_inherits_home_and_the_others_redirect(tmp_path):
    from agentkit.adapters import get_adapter

    assert get_adapter("cursor").home_policy == "inherit"
    for name in ("codex", "dsh", "fake"):
        assert get_adapter(name).home_policy == "redirect", name


def test_posix_lock_really_excludes_another_process(tmp_path):
    """flock 的真实争用：**另一个进程**也必须拿不到（不是"看起来拿到了"）。

    bridge 的单消费者保证完全靠这一条；只在自己进程里连开两次是测不出来的
    （同进程重复 flock 会成功，那是 flock 的语义）。
    """
    import time as _time

    from agentkit.platform.posix import PosixLockPlatform

    path = str(tmp_path / "consumer.lock")
    platform = PosixLockPlatform()
    holder = platform.acquire(path)
    try:
        child = (
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from agentkit.platform.posix import PosixLockPlatform\n"
            "try:\n"
            "    PosixLockPlatform().acquire(%r, timeout_s=0.3)\n"
            "except TimeoutError:\n"
            "    print('blocked')\n"
            "else:\n"
            "    print('acquired')\n"
        ) % (str(ROOT), path)
        started = _time.monotonic()
        output = subprocess.run([sys.executable, "-c", child], capture_output=True,
                                text=True, timeout=30)
        assert output.stdout.strip() == "blocked", output.stderr
        assert _time.monotonic() - started < 20
    finally:
        platform.release(holder)


def test_spawn_does_not_leak_the_prompt_file_descriptor(tmp_path):
    """prompt 走文件时，父进程的句柄必须在 Popen 之后关掉（否则长跑必漏 fd）。"""
    if not os.path.isdir("/dev/fd"):
        pytest.skip("no /dev/fd on this platform")
    platform = posix_platform.PosixProcessPlatform()
    workspace = str(tmp_path)
    env = platform.prepare_env(workspace=workspace)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("你好", encoding="utf-8")
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        proc = platform.spawn(
            Invocation(argv=[sys.executable, "-c", "print(1)"], env=env,
                       cwd=workspace, stdin_devnull=False, stdin_file=str(prompt))
        )
        proc.wait(timeout=10)
    after = len(os.listdir("/dev/fd"))
    assert after - before <= 1, (before, after)


def test_posix_line_reader_refuses_on_windows(monkeypatch):
    """结构性防混搭：POSIX 读取器不许在 Windows 上被实例化。"""
    from agentkit.platform.posix import PosixLineReader

    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(OSError) as excinfo:
        PosixLineReader(subprocess.Popen([sys.executable, "-c", "pass"],
                                         stdout=subprocess.PIPE, text=True))
    assert "ThreadLineReader" in str(excinfo.value)


def test_posix_line_reader_caps_a_giant_line_without_newline():
    """只吐字节不换行的进程不许把缓冲区撑爆：超限强制切"行"，交给字节预算去杀。"""
    from agentkit.platform.posix import PosixLineReader

    script = "import sys,time\nsys.stdout.write('x'*200000); sys.stdout.flush()\ntime.sleep(5)\n"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    reader = PosixLineReader(proc)
    reader.max_line_bytes = 4096
    try:
        kind, line = reader.next_line(10)
        assert kind == LINE
        assert len(line) > 4096            # 超限的那一段被切出来了
        assert reader.last_bytes > 4096    # 而且如实报了多少字节
    finally:
        reader.close()
        proc.kill()
        proc.wait(timeout=5)


def test_posix_terminate_kills_the_whole_process_group(tmp_path):
    """只杀父进程不够：孙进程要一起收掉（否则 reader 会挂到孙进程结束）。"""
    script = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    platform = posix_platform.PosixProcessPlatform()
    started = time.time()
    platform.terminate(proc, grace_s=0.2)
    assert proc.poll() is not None
    assert time.time() - started < 10
