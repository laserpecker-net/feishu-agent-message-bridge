"""身份层：一台机器只实例化一种身份（``as_user`` / ``as_bot``）。"""
from .base import Identity, IdentityError, TransportError
from .factory import MODES, build_identity, identity_block, identity_mode
from .user import UserIdentity
from .bot import BotIdentity

__all__ = ['Identity', 'IdentityError', 'TransportError', 'UserIdentity',
           'build_identity', 'identity_block', 'identity_mode', 'MODES']
