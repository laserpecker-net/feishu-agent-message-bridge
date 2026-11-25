"""身份抽象：一台机器只实例化**一种**身份（``as_user`` 或 ``as_bot``）。

设计见 ``docs/design-identity-as-user-as-bot.md``。这里只放接口与默认实现：

* 业务代码（service / collector / sender / 任务账本 / 干活端）只依赖本接口；
* 差异（"提到谁"、"要不要防自己说话"）收在实现类或默认实现里，
  **不允许**在业务代码里出现 ``if mode == 'bot'`` 这类分支。
"""
from abc import ABC
from typing import Iterable, List, Optional, Tuple


class TransportError(Exception):
    """发消息/取消息失败。名字沿用历史（外部有测试与服务按它捕获）。"""

    def __init__(self, message, kind='network', code=None, retryable=True):
        super().__init__(message)
        self.kind, self.code, self.retryable = kind, code, retryable


#: 更中性的别名：新代码可以用 ``IdentityError``，老代码继续捕 ``TransportError``（同一个类）。
IdentityError = TransportError


class Identity(ABC):
    """一种身份。

    约定：
    * ``needs_self_guard=True`` 表示"别回自己发的话"（个人账号需要；机器人天然不需要）；
    * ``start/stop`` 只管理**收消息通道**；``as_user`` 的采集由 service 的搜索轮询驱动，
      所以它的实现是空操作——这不影响语义（通道由谁驱动是平台细节）。
    """

    #: ``user`` | ``bot``
    mode = 'user'
    #: 是否需要"别回自己的话"守卫
    needs_self_guard = True

    # -- 通道 ---------------------------------------------------------------
    def start_channel(self, on_message=None):
        """启动收消息通道。``on_message`` 只给"推送型"身份用（如长连接）。

        名字带 ``_channel``：``as_user`` 实例上 ``self.stop`` 是停止事件（threading.Event），
        方法再叫 ``stop`` 就会被实例属性遮住——这是实现细节，但接口命名要避开它。
        """
        return None

    def stop_channel(self):
        return None

    def collect(self, scan_name, start_ms, end_ms):
        """对账式采集（可选能力）：返回 ``{'ids': [...], 'next_token': None, 'notice': None}``。

        为什么不叫"读消息通道"：``as_user`` 由 service 的搜索轮询驱动，没有可对账的窗口
        （搜索本身就是采集）；``as_bot`` 走长连接推送，**断线期间的缺口**需要按时间窗补，
        这里就是那个补口。返回 ``None`` = 本身份没有这个能力，调用方继续走原有搜索路径。
        **业务代码不需要判断模式**，只判断这个返回值。
        """
        return None

    # -- 发 -----------------------------------------------------------------
    def reply(self, original, body, uuid, before_send=None):
        """回复某条消息（挂在它下面）。"""
        raise NotImplementedError

    def send(self, chat_id, body, kind='text', dedupe_key=None):
        """群发 / 私聊。``kind='dm'`` 时 ``chat_id`` 理解成 open_id。"""
        raise NotImplementedError

    # -- 身份 ---------------------------------------------------------------
    def whoami(self) -> dict:
        raise NotImplementedError

    def describe(self) -> List[Tuple[str, str, bool]]:
        """体检用的只读行：``[(name, value, ok), ...]``。"""
        raise NotImplementedError

    # -- 业务代码只用下面这两个"通道/自证"能力，不判断模式 -----------------
    def self_sender_id(self):
        """**回读校验**用：这台机器发出去的消息，``sender.id`` 应该是谁。

        ``as_user`` = 本人 open_id；``as_bot`` = 机器人 app_id。校验逻辑本身不变，
        变的只是"以谁的名义发"——这正是身份层该封装的事。
        """
        return (getattr(self, 'config', None) or {}).get('target_open_id')

    def command_chat_ok(self, chat_id, *, self_chat_ids=(), chat_type=None, known_group=False):
        """命令通道判定：只有"主人对桥说话"的会话才算。

        ``as_user``：白名单自聊群；``as_bot``：与机器人的私聊。业务代码调用本方法，
        **不自己拼条件**，否则两套语义又会长回业务层里。
        """
        raise NotImplementedError

    def control_chat_ids(self, config, teach_chat=None):
        """需要**轮询**控制指令的会话（"下班/上班"）。

        ``as_user`` 个人账号收不到推送，只能轮询自聊；``as_bot`` 有长连接推 DM，
        不需要轮询——返回空集就是"不需要"，不是"不支持"。
        """
        return set()

    def owner_open_ids(self) -> Iterable[str]:
        """谁能批准、谁能发命令。默认就是配置里的 target_open_id。"""
        value = getattr(self, 'config', None) or {}
        target = value.get('target_open_id')
        block = (value.get('identity') or {})
        owners = list(block.get('owner_open_ids') or [])
        if target and target not in owners:
            owners.append(target)
        return owners

    def is_owner(self, sender_id: Optional[str]) -> bool:
        return bool(sender_id) and sender_id in set(self.owner_open_ids())

    def is_mentioned(self, message) -> bool:
        """有人在叫它吗？两种身份的判法不同，但语义相同。"""
        raise NotImplementedError
