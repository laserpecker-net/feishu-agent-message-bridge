"""兼容壳：``Transport`` 已搬进 :mod:`bridge.identity.user`（``UserIdentity``）。

保留本模块只为一件事：**老代码与现有测试不用改一行**
（``from bridge.transport import Transport`` 继续可用）。
新代码请用 ``bridge.identity.factory.build_identity(config)``，不要直接实例化。

两条纪律：
* 这里**不放任何实现**——不放实现就没有"两份实现"；
* 需要新增导出时，只 re-export，不要在本地再写一份同名函数。
"""
from .identity.user import (  # noqa: F401  （re-export：兼容旧导入点）
    AT_PLACEHOLDER,
    SEARCH_MIN_INTERVAL_SECONDS,
    UserIdentity,
    _AT_TAG,
    _AT_TEXT,
    _UNPARSED,
    _visible,
    canonical_text,
    mention_ids,
    mentioned,
    message_text,
    message_version,
    verify_reply,
    verify_reply_reason,
)
from .identity.base import IdentityError, TransportError  # noqa: F401  （错误类只此一份）

# 兼容壳还要把**模块对象**放回同一命名空间：老测试用
# ``patch('bridge.transport.time.monotonic')`` / ``patch('bridge.transport.subprocess.run')``
# 这类写法，patch 的是模块属性；这里 re-export 后打的仍是同一个模块对象，
# 所以实现类（identity/user.py）里的调用同样会命中，行为不变。
import hashlib  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import subprocess  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from datetime import datetime, timezone  # noqa: F401

#: 历史名字。等价于 :class:`bridge.identity.user.UserIdentity`。
Transport = UserIdentity
