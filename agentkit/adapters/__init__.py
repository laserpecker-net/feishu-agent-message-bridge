"""适配器层：Adapter（适配器）+ Registry（注册表）+ Factory（工厂函数）。

导入顺序有意义：``base`` 定义接口，``registry`` 提供登记，具体 adapter 模块在被导入时
用 ``@register`` 自我登记。因此本模块 **必须** 导入三个实现模块，否则注册表是空的。
"""

from __future__ import annotations

from .base import AgentAdapter, PrepareContext, RAW_TAIL_LINES, TurnState
from .registry import (
    ADAPTER_ENV,
    adapter_class,
    get_adapter,
    is_registered,
    list_adapters,
    register,
    registry_snapshot,
)

# 导入即注册（顺序无关，但必须发生）
from . import fake as _fake  # noqa: F401,E402
from . import legacy as _legacy  # noqa: F401,E402

__all__ = [
    "AgentAdapter",
    "PrepareContext",
    "TurnState",
    "RAW_TAIL_LINES",
    "ADAPTER_ENV",
    "adapter_class",
    "get_adapter",
    "is_registered",
    "list_adapters",
    "register",
    "registry_snapshot",
]
