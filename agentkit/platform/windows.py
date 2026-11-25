"""原生 Windows 平台实现（**默认关闭**，需要显式 opt-in）。

本仓对 Windows 的立场是 ``WINDOWS-DECISION.md``：bridge 本体不移植到原生 Windows
（依赖 flock / killpg / systemd），受支持形态是「WSL2 全内」或「原生 Windows 只做消费者」。

但"只做消费者"的那台 Windows 上，**AGENT 很可能是原生安装的**（Windows 版 Codex /
DSH）。所以本模块把"起一个 agent 进程"这部分做成可用的，同时：

* 默认 **拒绝实例化**（``get_platform()`` 只在 ``FEISHU_AGENTKIT_NATIVE_WINDOWS=1`` 时才放行）；
* 文件锁这一族**直接拒绝**（``PolicyRefusal``），不提供半吊子实现。

这样"抽象支持 Windows"不等于"悄悄改了部署形态"：要真的跑原生 Windows，必须先过
``WINDOWS-DECISION.md`` 的变更流程并在真机验证（该文档明确要求"未经批准不要自行发明
第三条拓扑"）。
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, Optional

from ..errors import PolicyRefusal
from .base import LockPlatform, ProcessPlatform
from .stream import ThreadLineReader

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def taskkill_command() -> List[str]:
    """``taskkill`` 的绝对路径优先：某些服务账号的 PATH 里没有 System32。"""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = os.path.join(root, "System32", "taskkill.exe")
    return [candidate] if os.path.isfile(candidate) else ["taskkill"]


class WindowsProcessPlatform(ProcessPlatform):
    """Windows 族的进程原语。

    ``popen`` / ``run_command`` 可注入，**只为了让这个族能在非 Windows 机器上被测到**：
    在 macOS 上跑不了真 ``taskkill``，但"发了几条命令、什么顺序、带没带 ``/T``"是可以
    断言的。真机验证交给 ``python3 -m agentkit selftest``（在 Windows 上跑一次）。
    """

    name = "windows"

    def __init__(self, popen=None, run_command=None):
        self._popen = popen or subprocess.Popen
        self._run = run_command or subprocess.run

    @property
    def supports_process_groups(self) -> bool:
        return True

    def spawn(self, inv) -> subprocess.Popen:
        flags = 0
        # 自成进程组：只有这样才能对子树发 CTRL_BREAK，也才不会被父控制台信号带走
        flags |= CREATE_NEW_PROCESS_GROUP
        if os.environ.get("FEISHU_AGENTKIT_HIDE_WINDOWS") == "1":
            flags |= CREATE_NO_WINDOW
        kwargs: Dict[str, object] = dict(
            cwd=inv.cwd,
            env=dict(inv.env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding=getattr(inv, "encoding", "utf-8"),
            errors="replace",
            creationflags=flags,
        )
        stdin_file = getattr(inv, "stdin_file", None)
        handle = None
        if stdin_file:
            handle = open(stdin_file, "rb")
            kwargs["stdin"] = handle
        elif getattr(inv, "stdin_devnull", True):
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            return self._popen(list(inv.argv), **kwargs)  # type: ignore[arg-type]
        finally:
            # 子进程已经 dup 出自己的句柄；父进程不关就是每个 turn 漏一个 fd
            if handle is not None:
                handle.close()

    def line_reader(self, proc: subprocess.Popen):
        # 管道在 Windows 上不可 select，只能线程 + 队列
        return ThreadLineReader(proc)

    def pid_alive(self, pid: int) -> Optional[bool]:
        """**不探活**：Windows 上没有安全的"只查询不动作"的 pid 检查。

        ``os.kill(pid, 0)`` 在 Windows 上会直接 TerminateProcess（见
        ``idempotency._default_pid_alive``）；``OpenProcess`` 需要 ctypes + 权限，
        且对"已退出但句柄未回收"的进程判断不可靠。返回 None 让幂等层退回时间阈值，
        语义上比"猜"更安全。
        """
        return None

    def harden_path(self, path: str, mode: int = 0o600, is_dir: bool = False) -> None:
        """Windows 上 0600/0700 不是权限：用 ACL 收掉继承，只留当前用户。"""
        user = os.environ.get("USERNAME") or ""
        # 拿不到用户名时**只收掉继承**，绝不退化成"给 Everyone 读" ——
        # prompt 文件里有用户消息，宽授权比少授权危险得多。
        command = ["icacls", path, "/inheritance:r"]
        if user:
            command += ["/grant:r", "%s:F" % user]
        try:
            self._run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass  # FAT32/exFAT 没有 ACL；收紧失败不应让 turn 失败

    def terminate(self, proc: subprocess.Popen, grace_s: float = 5.0) -> None:
        """Windows 没有 SIGTERM 的等价物：``terminate()`` 只结束主进程。

        用 ``taskkill /T`` 才能连子进程一起收掉；``/F`` 是必要的，因为 GUI 子进程常常
        不吃 WM_CLOSE。先试不带 ``/F``（给 CLI 一次 flush 的机会），再 ``/F``。
        """
        if proc.poll() is not None:
            return
        for extra, wait_s in (((), grace_s), (("/F",), 2.0)):
            if proc.poll() is not None:
                return
            try:
                self._run(
                    taskkill_command() + ["/T", "/PID", str(proc.pid)] + list(extra),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            deadline = time.time() + wait_s
            while time.time() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.05)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass


class WindowsLockPlatform(LockPlatform):
    """原生 Windows 上的跨进程锁：**不提供**。

    不是"还没做"，是"故意不做"：bridge 的租约/幂等依赖 flock 的语义（进程死亡即释放），
    换成 ``msvcrt.locking`` 或命名互斥体后语义会变（句柄泄漏即永久锁），这属于拓扑变更，
    必须走 ``WINDOWS-DECISION.md`` 的评审，而不是在代码里悄悄替换。
    """

    name = "windows-unsupported"

    def acquire(self, path: str, timeout_s: float = 10.0) -> object:
        raise PolicyRefusal(
            "no cross-process locking on native Windows by design; run the bridge in WSL2 "
            "or on macOS, or use the HTTP event gateway (see WINDOWS-DECISION.md)"
        )

    def release(self, handle: object) -> None:  # pragma: no cover
        raise PolicyRefusal("nothing to release: acquire() always refuses on native Windows")
