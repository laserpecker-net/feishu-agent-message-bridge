"""适配器注册表 + 工厂函数。

为什么需要注册表：本仓现状是"换一个 adapter = 换一个入口脚本"
（``dsh-consumer.py`` / ``codex-consumer.py`` / ``cursor-consumer.py`` 各自 ``main()``），
调用方必须知道文件名。注册表把"选哪个"从文件系统搬到名字空间，于是：

* 可以用环境变量选（``FEISHU_AGENT_ADAPTER=dsh``）；
* 可以列出全部可用 adapter 及其能力（给 ``--check`` 输出用）；
* 新增一个 AGENT 只需要"写一个类 + 注册"，不需要再抄一份 700 行的监督循环。

发现机制刻意保守：**只认显式注册**，不做目录扫描、不用 entry points。
理由与本仓"唯一权威文档"的风格一致 —— 隐式发现会让"为什么跑的是这个 adapter"
变得不可回答，而这是排查 agent 行为错误时问的第一个问题。
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Dict, Iterable, List, Optional, Type

from ..errors import AdapterNotFound
from .base import AgentAdapter

_LOCK = threading.Lock()
_REGISTRY: Dict[str, Type[AgentAdapter]] = {}
_BY_CLASS: Dict[Type[AgentAdapter], str] = {}

#: 环境变量选 adapter 的名字（与现存的 ``FEISHU_CONSUMER_*`` 前缀风格一致）
ADAPTER_ENV = "FEISHU_AGENT_ADAPTER"


def register(cls: Type[AgentAdapter]) -> Type[AgentAdapter]:
    """类装饰器：登记一个 adapter（同时登记它的别名）。"""
    if not getattr(cls, "name", ""):
        raise ValueError("adapter class %r has no name" % (cls,))
    with _LOCK:
        for key in (cls.name,) + tuple(getattr(cls, "aliases", ()) or ()):
            normalized = str(key).strip().lower()
            if not normalized:
                continue
            existing = _REGISTRY.get(normalized)
            if existing is not None and existing is not cls:
                raise ValueError(
                    "adapter name %r already registered by %r" % (normalized, existing)
                )
            _REGISTRY[normalized] = cls
        _BY_CLASS[cls] = cls.name
    return cls


def list_adapters() -> List[str]:
    """返回**主名**的排序列表（不含别名）。"""
    with _LOCK:
        return sorted(set(_BY_CLASS.values()))


def registry_snapshot() -> Dict[str, str]:
    """名字 → 类名，给 ``--check``/诊断输出用。"""
    with _LOCK:
        return {key: cls.__name__ for key, cls in sorted(_REGISTRY.items())}


def adapter_class(name: Optional[str] = None) -> Type[AgentAdapter]:
    """按名字取类；``name`` 为空时读 ``FEISHU_AGENT_ADAPTER``，再缺省为 ``default``。"""
    wanted = (name or os.environ.get(ADAPTER_ENV) or "").strip().lower()
    if not wanted:
        raise AdapterNotFound(
            "no adapter named and %s is unset; known adapters: %s"
            % (ADAPTER_ENV, ", ".join(list_adapters()) or "<none>")
        )
    with _LOCK:
        cls = _REGISTRY.get(wanted)
    if cls is None:
        raise AdapterNotFound(
            "unknown adapter %r; known: %s" % (wanted, ", ".join(list_adapters()) or "<none>")
        )
    return cls


def get_adapter(name: Optional[str] = None, **kwargs) -> AgentAdapter:
    """工厂函数：造一个 adapter 实例。

    这是"工厂方法"意义上的工厂 —— 只负责**创建一个产品**；跨产品的族一致性由
    ``platform.get_platform()``（抽象工厂）保证。
    """
    return adapter_class(name)(**kwargs)


def is_registered(name: str) -> bool:
    with _LOCK:
        return str(name).strip().lower() in _REGISTRY


def reset_for_tests() -> None:
    """仅测试用：清空注册表（生产代码不要调用）。"""
    with _LOCK:
        _REGISTRY.clear()
        _BY_CLASS.clear()
