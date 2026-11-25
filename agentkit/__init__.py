"""agentkit — 跨操作系统 / 跨 AGENT 的执行底座。

设计模式（三者组合，不是单一工厂）：

* **Adapter（适配器）** —— 每个 AGENT CLI 一个 adapter，把"怎么起它、它的流长什么样、
  怎么从里面取出最终答案"适配成同一套接口（``AgentAdapter``）。
* **Abstract Factory（抽象工厂）** —— 操作系统这一族相关原语（进程组、终止方式、路径、
  编码、文件锁）收成 ``ProcessPlatform`` / ``LockPlatform`` 两个抽象工厂，由
  ``get_platform()`` 按当前 OS 一次造出一整套，业务代码不再出现 ``if sys.platform``。
* **Template Method（模板方法）** —— 一次 turn 的骨架固定在上面两层之上
  （``TurnRunner.run()``）：占位幂等 → 快照 → 组环境 → 起进程 → 逐行扫描 →
  deny 兜底 → 超时兜底 → 产物比对 → 判定终态。adapter 只实现钩子，顺序无法被改错。

为什么不只有工厂：工厂解决"造哪个"，适配器解决"怎么对接"，模板方法解决"别把顺序搞错"。
三者的边界就是本包的三层目录：``platform/``（抽象工厂）、``adapters/``（适配器 + 注册表）、
``turn.py``（模板方法）。

在此之上还有两个"接入层"文件（不属于上面三类，只是把底座接到既有进程上）：

* ``session.py`` —— 多轮续跑的会话指针（跨进程记住"下一轮接哪个 session"）；
* ``driver.py``  —— 实现 ``consumer_runtime`` 要的
  ``build_agent(home) -> generate(payload, event, stop_event) -> str``。

自检：``python3 -m agentkit selftest`` 会在当前机器上真跑一遍平台层。
"""

from .types import (  # noqa: F401
    Capabilities,
    DenyPolicy,
    Invocation,
    Outcome,
    StreamEvent,
    TurnRequest,
    TurnStatus,
)
from .errors import (  # noqa: F401
    AgentKitError,
    AdapterNotFound,
    PolicyRefusal,
    TurnInProgress,
    UnsupportedPlatform,
)
from .platform import get_platform, current  # noqa: F401
from .turn import TurnRunner  # noqa: F401
from .adapters import get_adapter, list_adapters, register  # noqa: F401

__all__ = [
    "Capabilities",
    "DenyPolicy",
    "Invocation",
    "Outcome",
    "StreamEvent",
    "TurnRequest",
    "TurnStatus",
    "AgentKitError",
    "AdapterNotFound",
    "PolicyRefusal",
    "TurnInProgress",
    "UnsupportedPlatform",
    "get_platform",
    "current",
    "TurnRunner",
    "get_adapter",
    "list_adapters",
    "register",
]

__version__ = "0.1.0"
