"""Windows 族平台层：在 macOS 上也能验的部分。

真 ``taskkill`` 只能在 Windows 上跑，所以这里注入 ``run_command`` / ``popen``，
断言**具体命令与创建标志**（这是最容易写错、也最容易在真机上炸的部分）。
真机验证走 ``python3 -m agentkit selftest``。
"""

import io
import os
import subprocess

import pytest

from agentkit.errors import PolicyRefusal
from agentkit.platform import windows as windows_mod
from agentkit.platform.stream import ThreadLineReader
from agentkit.platform.windows import (
    CREATE_NEW_PROCESS_GROUP,
    WindowsLockPlatform,
    WindowsProcessPlatform,
)
from agentkit.types import Invocation


class FakeProc(object):
    """可控的假进程：``poll()`` 在被打够 N 次之后才算死。"""

    def __init__(self, pid=4242, die_after_calls=0, die_when=None):
        self.pid = pid
        self.returncode = None
        self._die_after = die_after_calls
        self._die_when = die_when
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._die_after is not None and self._polls > self._die_after:
            self.returncode = 0
        if self._die_when is not None and self._die_when():
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -1

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


def make_platform(records, proc, popen_records=None):
    def run(command, **kwargs):
        records.append((list(command), dict(kwargs)))
        return subprocess.CompletedProcess(args=command, returncode=0)

    def popen(argv, **kwargs):
        if popen_records is not None:
            popen_records.append((list(argv), dict(kwargs)))
        return proc

    return WindowsProcessPlatform(popen=popen, run_command=run)


def test_spawn_creates_new_process_group():
    popens = []
    platform = make_platform([], FakeProc(), popen_records=popens)
    inv = Invocation(argv=["codex.exe", "--json"], env={"PATH": "C:\\"}, cwd="C:\\ws")
    platform.spawn(inv)
    argv, kwargs = popens[0]
    assert argv == ["codex.exe", "--json"]
    # 自成进程组 = 后面 taskkill /T 才有意义
    assert kwargs["creationflags"] & CREATE_NEW_PROCESS_GROUP
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["cwd"] == "C:\\ws"
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_spawn_feeds_the_prompt_file_as_stdin(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("你好", encoding="utf-8")
    popens = []
    platform = make_platform([], FakeProc(), popen_records=popens)
    platform.spawn(Invocation(argv=["codex.exe"], env={}, cwd=str(tmp_path),
                              stdin_devnull=False, stdin_file=str(prompt)))
    _argv, kwargs = popens[0]
    handle = kwargs["stdin"]
    # 平台起完进程就该关掉父进程的句柄（子进程已 dup 自己的），否则每个 turn 漏一个 fd
    assert handle.name == str(prompt)
    assert handle.closed is True


def test_terminate_escalates_to_force_after_grace():
    records = []
    # 一次 taskkill 不够、必须 /F：用"已经发了 2 条 taskkill 才死"来模拟
    proc = FakeProc(die_after_calls=None, die_when=lambda: len(records) >= 2)
    platform = make_platform(records, proc)
    platform.terminate(proc, grace_s=0.01)
    assert len(records) >= 2, records
    first, second = records[0][0], records[1][0]
    assert first[:3] == ["taskkill", "/T", "/PID"]
    assert "/F" not in first          # 先给一次 flush 的机会
    assert second[:3] == ["taskkill", "/T", "/PID"]
    assert "/F" in second             # 再强杀


def test_terminate_is_a_noop_when_already_exited():
    records = []
    proc = FakeProc(die_after_calls=-1)  # 立刻就是死的
    platform = make_platform(records, proc)
    platform.terminate(proc, grace_s=0.01)
    assert records == []


def test_lock_refuses_by_design():
    with pytest.raises(PolicyRefusal) as excinfo:
        WindowsLockPlatform().acquire("C:\\lock")
    assert "WINDOWS-DECISION.md" in str(excinfo.value)


def test_env_keeps_windows_specific_variables_but_redirects_profile(tmp_path):
    platform = WindowsProcessPlatform(popen=lambda *a, **k: FakeProc())
    base = {
        "PATH": "C:\\Windows\\system32",
        "SystemRoot": "C:\\Windows",
        "COMSPEC": "C:\\Windows\\system32\\cmd.exe",
        "PATHEXT": ".EXE;.CMD",
        "WINDIR": "C:\\Windows",
        "USERPROFILE": "C:\\Users\\someone",
        "APPDATA": "C:\\Users\\someone\\AppData",
        "OPENAI_API_KEY": "sk-must-not-leak",
    }
    env = platform.prepare_env(base=base, workspace=str(tmp_path))
    assert env["USERPROFILE"].startswith(str(tmp_path))  # 重定向了
    assert env["SystemRoot"] == "C:\\Windows"            # 但这些必须留下
    assert env["COMSPEC"].endswith("cmd.exe")
    assert env["PATHEXT"] == ".EXE;.CMD"
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHONUTF8"] == "1"                      # 钉死 UTF-8（cp936 的坑）


def test_harden_path_uses_an_acl_not_a_mode_bit(monkeypatch):
    """Windows 上 0600 不是权限：真正的收紧手段是 icacls 收掉继承。"""
    monkeypatch.setenv("USERNAME", "apple")
    records = []
    platform = make_platform(records, FakeProc())
    platform.harden_path("C:\\ws\\prompt.txt", 0o600)
    argv, _kwargs = records[0]
    assert argv[0] == "icacls"
    assert argv[1] == "C:\\ws\\prompt.txt"
    assert "/inheritance:r" in argv          # 先去掉继承
    assert "/grant:r" in argv                # 再只授当前用户
    assert "apple:F" in argv


def test_harden_path_never_grants_everyone_when_the_user_is_unknown(monkeypatch):
    """拿不到用户名时只收继承，绝不退化成"所有人可读"。"""
    monkeypatch.delenv("USERNAME", raising=False)
    records = []
    platform = make_platform(records, FakeProc())
    platform.harden_path("C:\\ws\\prompt.txt", 0o600)
    argv, _kwargs = records[0]
    assert "/inheritance:r" in argv
    assert "/grant:r" not in argv


def test_harden_path_never_fails_the_turn():
    """FAT32/exFAT 没有 ACL：收紧失败不能被当成 turn 失败。"""

    def boom(command, **kwargs):
        raise OSError("icacls not available")

    platform = WindowsProcessPlatform(popen=lambda *a, **k: FakeProc(), run_command=boom)
    platform.harden_path("C:\\ws\\x", 0o600)  # 不抛即通过


def test_line_reader_is_thread_based_not_selectors():
    """Windows 的 selectors 只支持 socket：这一族必须换成线程读取器。"""
    proc = FakeProc()
    proc.stdout = io.StringIO('{"a":1}\n')
    reader = WindowsProcessPlatform().line_reader(proc)
    assert isinstance(reader, ThreadLineReader)
    kind, line = reader.next_line(5)
    assert (kind, line) == ("line", '{"a":1}')
    assert reader.next_line(5)[0] == "eof"
