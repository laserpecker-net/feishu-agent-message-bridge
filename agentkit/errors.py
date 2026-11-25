"""agentkit 的异常族。

原则：**失败方向必须明确**。平台不支持、策略拒绝这类"我不做"的错误，一律在
"产生副作用之前"抛出（fail-closed），不允许以返回值形式静默降级。
"""


class AgentKitError(Exception):
    """所有 agentkit 异常的基类。"""


class UnsupportedPlatform(AgentKitError):
    """当前操作系统没有受支持的平台实现。

    本仓对 Windows 的立场见 ``WINDOWS-DECISION.md``（唯一权威）：bridge 本体不移植到
    原生 Windows。agentkit 因此把"能不能在原生 Windows 上起 AGENT"做成**显式开关**，
    默认拒绝，而不是检测到 nt 就悄悄换一套行为。
    """


class PolicyRefusal(AgentKitError):
    """平台或适配器拒绝执行某操作（例如原生 Windows 上取文件锁）。

    与 :class:`UnsupportedPlatform` 的区别：平台存在，但这个具体能力被策略关闭。
    """


class AdapterNotFound(AgentKitError):
    """注册表里没有这个 agent 名字（或它导入失败）。"""


class TurnInProgress(AgentKitError):
    """同一个 turn key 的上一轮还在跑。

    按 UHP 的语义（``tasks.md`` §6），重复提交**不能**返回冲突让调用方自己决定，
    而应该等第一次的结果。本异常只在等待窗口耗尽后抛，且携带已有 turn 的信息。
    """

    def __init__(self, turn_key: str, record_path: object = None):
        self.turn_key = turn_key
        self.record_path = record_path
        super().__init__(
            "turn %r is still running; waited the full window without a terminal outcome"
            % (turn_key,)
        )
