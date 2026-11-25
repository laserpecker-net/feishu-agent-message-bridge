"""流读取策略：**这是"兼容不同操作系统"的真正难点**。

本模块**只放可移植的部分**：抽象基类 + 线程读取器。POSIX 的 ``selectors`` 读取器住在
``platform/posix.py`` —— 这样"POSIX-only 代码只出现在 POSIX 模块里"是一条可被静态检查
（见 ``tests/test_agentkit_portability.py``）保住的规则，而不是口头约定。

为什么单独抽出来：POSIX 上可以用 ``selectors`` 对管道做非阻塞读；而 **Windows 的
``selectors`` 只支持 socket，不支持管道**（``SelectSelector`` 对非 socket 句柄直接抛
``OSError``）。所以"边跑边读、带超时"这件事在两个平台上必须是两种实现 —— 这正是
抽象工厂存在的意义：业务代码只调 ``reader.next_line(timeout)``，永远不写
``if os.name == 'nt'``。
"""

from __future__ import annotations

import subprocess
import threading
from abc import ABC, abstractmethod
from typing import Tuple

#: ``next_line`` 的三种结果
LINE = "line"
TIMEOUT = "timeout"
EOF = "eof"


class LineReader(ABC):
    """带超时的逐行读取器。三个实现细节都要保证：

    * **不丢半行**：字节到达不等于行到达，必须自己按 ``\\n`` 切分并缓存残余；
    * **超时可控**：调用方要靠它实现"每 N 秒检查一次 deadline / stop_event"；
    * **EOF 明确**：流结束与"暂时没数据"必须可区分，否则 turn 永远不结束。

    另有 ``last_bytes``：**最近一次返回的行在线上占了多少字节**。输出上限必须按字节
    而不是按字符计（中文一个字 3 字节，按 len(str) 计数会把上限放大 3 倍），而
    "从字节切行"这件事只有读取器自己知道真相。
    """

    #: 最近一次 ``next_line`` 返回 LINE 时消耗的原始字节数（含换行符）
    last_bytes: int = 0

    @abstractmethod
    def next_line(self, timeout_s: float) -> Tuple[str, str]:
        """返回 ``(LINE, 文本)`` / ``(TIMEOUT, '')`` / ``(EOF, '')``。"""

    @abstractmethod
    def close(self) -> None:
        ...


class ThreadLineReader(LineReader):
    """线程 + 队列的读取器：Windows 上的唯一正确做法（管道不可 select）。

    读线程用**阻塞** ``readline()``（在 Windows 上阻塞读管道是安全的），把行推进队列；
    调用方用 ``queue.get(timeout=...)`` 实现超时。EOF 用一个哨兵显式标记，避免"空队列"
    与"流结束"被混淆。
    """

    _SENTINEL = object()

    def __init__(self, proc: subprocess.Popen, encoding: str = "utf-8"):
        if proc.stdout is None:  # pragma: no cover
            raise ValueError("process has no stdout pipe")
        import queue

        self._queue: "queue.Queue[object]" = queue.Queue()
        self._proc = proc
        self._encoding = encoding
        self._closed = False
        self._thread = threading.Thread(target=self._pump, name="agentkit-reader", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        stream = self._proc.stdout
        try:
            if stream is None:  # pragma: no cover
                return
            for raw in iter(stream.readline, ""):
                self._queue.put(raw.rstrip("\r\n"))
            # 某些 CLI 最后一行没有换行符：把残余也交出去
            tail = getattr(stream, "read", lambda: "")()
            if tail:
                self._queue.put(str(tail).rstrip("\r\n"))
        except (ValueError, OSError):  # pragma: no cover - 进程被杀时会走到
            pass
        finally:
            self._queue.put(self._SENTINEL)

    def next_line(self, timeout_s: float) -> Tuple[str, str]:
        import queue

        try:
            item = self._queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return TIMEOUT, ""
        if item is self._SENTINEL:
            self._queue.put(self._SENTINEL)  # 让后续调用继续看到 EOF
            self.last_bytes = 0
            return EOF, ""
        text = str(item)
        # readline() 已经解码过，只能按 UTF-8 复算；+1 是它吃掉的行结束符
        self.last_bytes = len(text.encode(self._encoding, "replace")) + 1
        return LINE, text

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdout.close()  # type: ignore[union-attr]
        except (OSError, ValueError):  # pragma: no cover
            pass
