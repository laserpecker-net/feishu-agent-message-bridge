"""POSIX 平台实现（production 路径，行为与现有代码保持一致）。

三个关键点都来自实测而不是洁癖：

* ``start_new_session=True`` —— 子进程自成进程组，取消/超时才可能 ``killpg`` 得到
  它派生的 shell 和孙进程；
* ``stdin=DEVNULL`` —— 继承 stdin 的 CLI 有可能**一个事件都不产生**（HarnessRouter 在
  vtcode 0.177.0 上实测：同样的 argv，关掉 stdin 后几秒就答）；
* ``stderr=STDOUT`` —— 有些 CLI 把错误写到 stderr，而 stdout 才是它的"事件流"，
  分开读会丢掉唯一的诊断信息。
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import selectors
import subprocess
import time
from typing import Dict, Optional

from .base import LockPlatform, ProcessPlatform
from .stream import EOF, LINE, TIMEOUT, LineReader


class PosixLineReader(LineReader):
    """``selectors`` + ``os.read`` + 自建行缓冲。

    刻意不用 ``proc.stdout.readline()``：文本层的 readline 自带缓冲，和 selectors 混用会
    出现"select 说可读、readline 又阻塞"的经典坑。
    """

    #: 单行上限。超了就当作"一行"切出去，让上层按字节预算杀掉这个 turn。
    #: 不设上限的话，一个只吐字节不换行的进程可以把这个缓冲区撑到 OOM。
    max_line_bytes = 8 * 1024 * 1024

    def __init__(self, proc: subprocess.Popen, encoding: str = "utf-8", read_size: int = 65536):
        if os.name == "nt":  # 结构性防混搭：Windows 的 selectors 只支持 socket
            raise OSError(
                "PosixLineReader needs selectable pipes; on Windows use ThreadLineReader "
                "(agentkit.platform.windows.WindowsProcessPlatform)"
            )
        if proc.stdout is None:  # pragma: no cover - 调用方保证 PIPE
            raise ValueError("process has no stdout pipe")
        self._proc = proc
        self._fd = proc.stdout.fileno()
        os.set_blocking(self._fd, False)
        self._encoding = encoding
        self._size = read_size
        self._buffer = b""
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._fd, selectors.EVENT_READ)
        self._eof = False

    def next_line(self, timeout_s: float) -> Tuple[str, str]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line, self._buffer = self._buffer[:newline], self._buffer[newline + 1:]
                self.last_bytes = newline + 1
                return LINE, line.decode(self._encoding, "replace").rstrip("\r")
            if len(self._buffer) > self.max_line_bytes:
                # 没换行但已经过载：强制切出一"行"（可能不是完整 json），
                # 让上层的 output_budget 生效并杀掉进程，而不是继续吃内存。
                chunk, self._buffer = self._buffer, b""
                self.last_bytes = len(chunk)
                return LINE, chunk.decode(self._encoding, "replace")
            if self._eof:
                if self._buffer:
                    rest, self._buffer = self._buffer, b""
                    self.last_bytes = len(rest)
                    return LINE, rest.decode(self._encoding, "replace").rstrip("\r")
                self.last_bytes = 0
                return EOF, ""
            if not self._selector.select(timeout=max(0.0, timeout_s)):
                return TIMEOUT, ""
            try:
                chunk = os.read(self._fd, self._size)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                chunk = b""
            if not chunk:
                self._eof = True
                continue
            self._buffer += chunk

    def close(self) -> None:
        try:
            self._selector.unregister(self._fd)
        except (KeyError, ValueError, OSError):
            pass
        try:
            self._selector.close()
        except OSError:  # pragma: no cover
            pass


class PosixProcessPlatform(ProcessPlatform):
    name = "posix"

    def spawn(self, inv) -> subprocess.Popen:
        kwargs: Dict[str, object] = dict(
            cwd=inv.cwd,
            env=dict(inv.env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding=getattr(inv, "encoding", "utf-8"),
            errors="replace",
            start_new_session=True,
        )
        stdin_file = getattr(inv, "stdin_file", None)
        handle = None
        if stdin_file:
            handle = open(stdin_file, "rb")
            kwargs["stdin"] = handle
        elif getattr(inv, "stdin_devnull", True):
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            return subprocess.Popen(list(inv.argv), **kwargs)
        finally:
            # 子进程已 dup 出自己的句柄；父进程不关就是每个 turn 漏一个 fd
            if handle is not None:
                handle.close()  # type: ignore[arg-type]

    def line_reader(self, proc: subprocess.Popen):
        return PosixLineReader(proc)

    def pid_alive(self, pid: int):
        if pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    def terminate(self, proc: subprocess.Popen, grace_s: float = 5.0) -> None:
        if proc.poll() is not None:
            return
        pgid: Optional[int] = None
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None
        # 先礼后兵：SIGTERM 整组 → 等 grace → SIGKILL 整组
        for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 2.0)):
            if proc.poll() is not None:
                return
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.time() + wait_s
            while time.time() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.05)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover - 宁可留下也不卡死调用方
            pass


class PosixLockPlatform(LockPlatform):
    """基于 ``flock`` 的排他锁（与 bridge 现有语义一致：进程退出即释放）。"""

    name = "posix-flock"

    def acquire(self, path: str, timeout_s: float = 10.0) -> object:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = open(path, "a+b")
        deadline = time.time() + timeout_s
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    handle.close()
                    raise
                if time.time() >= deadline:
                    handle.close()
                    raise TimeoutError("could not lock %s within %.1fs" % (path, timeout_s))
                time.sleep(0.05)

    def release(self, handle: object) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            handle.close()  # type: ignore[attr-defined]
