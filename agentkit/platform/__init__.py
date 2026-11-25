"""平台层的入口：``get_platform()`` 就是那个"工厂"。

一处策略、一处开关。业务代码永远不写 ``if os.name == 'nt'``，只问工厂要一整套原语。
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from ..errors import UnsupportedPlatform
from .base import LockPlatform, ProcessPlatform, WorkspacePaths

_CACHE: Dict[str, object] = {}

#: 原生 Windows 的显式放行开关。默认关闭的理由见 ``WINDOWS-DECISION.md``（唯一权威）：
#: bridge 本体依赖 flock / killpg / systemd，原生 Windows 不在受支持拓扑内。
NATIVE_WINDOWS_FLAG = "FEISHU_AGENTKIT_NATIVE_WINDOWS"


def get_platform(force: Optional[str] = None) -> ProcessPlatform:
    """返回当前操作系统的进程族实现。

    ``force`` 只给测试用（``'posix'`` / ``'windows'``），生产路径不传。
    """
    key = force or ("windows" if os.name == "nt" else "posix")
    cached = _CACHE.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    if key == "posix":
        from .posix import PosixProcessPlatform

        platform: ProcessPlatform = PosixProcessPlatform()
    elif key == "windows":
        if force is None and os.environ.get(NATIVE_WINDOWS_FLAG) != "1":
            raise UnsupportedPlatform(
                "native Windows agent execution is opt-in and off by default. "
                "The bridge itself is POSIX-only (flock / killpg / systemd): see "
                "WINDOWS-DECISION.md. Supported topologies are (A) everything inside "
                "WSL2, or (B) native Windows as an event consumer only, via the HTTP "
                "gateway. To run a natively-installed agent from this machine set "
                "%s=1 -- but that is a topology change and needs the review that "
                "WINDOWS-DECISION.md documents." % (NATIVE_WINDOWS_FLAG,)
            )
        from .windows import WindowsProcessPlatform

        platform = WindowsProcessPlatform()
    else:  # pragma: no cover - 防御性
        raise UnsupportedPlatform("unknown platform %r" % (key,))

    _CACHE[key] = platform
    return platform


def get_lock_platform(force: Optional[str] = None) -> LockPlatform:
    """返回当前操作系统的跨进程锁实现。"""
    key = ("windows" if os.name == "nt" else "posix") if force is None else force
    cache_key = "lock:" + key
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    if key == "posix":
        from .posix import PosixLockPlatform

        lock: LockPlatform = PosixLockPlatform()
    elif key == "windows":
        from .windows import WindowsLockPlatform

        lock = WindowsLockPlatform()
    else:  # pragma: no cover
        raise UnsupportedPlatform("unknown platform %r" % (key,))
    _CACHE[cache_key] = lock
    return lock


def current() -> Optional[ProcessPlatform]:
    """尽力返回当前平台；不支持时返回 None（给能力报告用，不抛异常）。"""
    try:
        return get_platform()
    except UnsupportedPlatform:
        return None


def describe() -> Dict[str, object]:
    """平台能力报告（纯数据，无 I/O），对应 UHP 的 "capabilities 必须先读再假设"。"""
    platform = current()
    return {
        "os": os.name,
        "platform": platform.name if platform else "unsupported",
        "process_groups": bool(platform and platform.supports_process_groups),
        "native_windows_opt_in": os.environ.get(NATIVE_WINDOWS_FLAG) == "1",
        "workspace_paths": os.path.join("<workspace>", ".agentkit"),
    }


__all__ = [
    "get_platform",
    "get_lock_platform",
    "current",
    "describe",
    "ProcessPlatform",
    "LockPlatform",
    "WorkspacePaths",
    "NATIVE_WINDOWS_FLAG",
]
