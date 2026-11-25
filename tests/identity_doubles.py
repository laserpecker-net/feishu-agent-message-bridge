"""测试替身用的身份接口（user 模式）：让 Fake transport 满足 Service 依赖的 6 个方法。

为什么不改 Service 去容忍缺方法：那等于把"身份层"这层契约又糊掉。替身补方法是一次性的，
而业务代码里加兼容分支会永久留在主干里。
"""
from bridge.identity.user import mentioned


class UserModeDouble:
    """user 模式替身：owner/open_id/命令通道都按"本人 + 白名单"的老语义。"""

    mode = 'user'
    needs_self_guard = True
    owner_id = 'ou_target'

    # -- 身份 ---------------------------------------------------------------
    def owner_open_ids(self):
        return [self.owner_id]

    def is_owner(self, sender_id):
        return bool(sender_id) and sender_id == self.owner_id

    def is_mentioned(self, message):
        return mentioned(message, self.owner_id)

    def self_sender_id(self):
        return self.owner_id

    # -- 通道 ---------------------------------------------------------------
    def command_chat_ok(self, chat_id, *, self_chat_ids=(), chat_type=None, known_group=False):
        return chat_id in set(self_chat_ids or ()) or not known_group

    def control_chat_ids(self, config, teach_chat=None):
        chats = set((config or {}).get('self_chat_ids') or [])
        if teach_chat:
            chats.add(teach_chat)
        return chats
