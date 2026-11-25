"""按配置实例化一种身份。

**一台机器只有一种身份**（主人 2026-11-20 硬原则）：``as_user`` 与 ``as_bot`` 不允许并存。
配置矛盾的处置方式是**启动失败**（不静默、不按群路由兜底），因为并存必然导致同一个群里双答。

配置形状（见 ``docs/design-identity-as-user-as-bot.md`` §3）::

    {"identity": {"mode": "user"}}              # 老配置没有这个块 → 默认 user
    {"identity": {"mode": "bot", "bot": {...}}} # as_bot（PR-2 实现）

``owner_open_ids`` 缺省时取 ``target_open_id``（个人账号就是本人）。
"""
import threading

from .base import Identity, TransportError
from .user import UserIdentity
from .bot import BotIdentity

__all__ = ['build_identity', 'identity_block', 'identity_mode', 'Identity',
           'UserIdentity', 'BotIdentity', 'TransportError', 'MODES']

#: 允许的取值。加新模式时：先在这里加、再写实现类、再改文档，三处一起。
MODES = ('user', 'bot')


def identity_block(config):
    block = (config or {}).get('identity')
    return block if isinstance(block, dict) else {}


def identity_mode(config):
    raw = identity_block(config).get('mode')
    if raw is None:
        return 'user'  # 老配置零迁移
    if not isinstance(raw, str) or raw.strip().lower() not in MODES:
        raise RuntimeError('identity.mode 只能是 %s 之一，当前是 %r'
                           % (' / '.join(MODES), raw))
    return raw.strip().lower()


def build_identity(config, stop=None):
    """返回一个 :class:`Identity` 实例。配置矛盾时**抛错**（调用方据此启动失败）。"""
    config = config or {}
    mode = identity_mode(config)
    block = identity_block(config)
    bot = block.get('bot') or {}

    if mode == 'user' and isinstance(bot, dict) and bot.get('app_id'):
        # 硬原则：一台机器一种身份。既配了用户态又配了机器人身份，说明配置来自两次不同的意图，
        # 猜哪个都会导致"同一个群两个人格同时说话"——宁可起不来。
        raise RuntimeError(
            '配置矛盾：identity.mode=user，却同时配了 identity.bot.app_id=%r。'
            '一台机器只能有一种身份：删掉 identity.bot 整块，或把 identity.mode 改成 bot。'
            % (bot.get('app_id'),))

    stop = stop if stop is not None else threading.Event()
    if mode == 'bot':
        return BotIdentity(config, stop)

    return UserIdentity(config, stop)

def _mask(value, keep=6):
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= keep + 6:
        return text[:keep] + '…'
    return text[:keep] + '…' + text[-4:]


def doctor_checks(config, home, identity_info):
    """身份层的体检项（**按模式在实现类里决定**，业务代码里不写 mode 判断）。

    为什么放这里：`as_user` 判"用户 token 有效"，`as_bot` 判"bot 身份就绪 + token 可读 +
    回环网关在听"——这是三种完全不同的判据，散在 cli.py 里就会长出
    ``if mode == 'bot'``（那正是"两套实现"的起点）。业务层只负责把这些行拼进体检表。
    """
    identity = identity_info if isinstance(identity_info, dict) else {}
    block = (config or {}).get('identity') or {}
    mode = str(block.get('mode') or 'user').lower()
    if mode == 'bot':
        return _bot_doctor_checks(config, home, identity)
    return _user_doctor_checks(identity)


def _user_doctor_checks(identity):
    token_status = str(identity.get('tokenStatus') or '').lower()
    ok = (not identity.get('error') and identity.get('available') is not False
          and token_status not in ('invalid', 'expired', 'missing'))
    return [{'name': 'authenticated', 'status': 'ok' if ok else 'fail',
             'detail': 'tokenStatus=%s available=%s'
                       % (identity.get('tokenStatus'), identity.get('available'))}]


def _bot_doctor_checks(config, home, identity):
    import stat
    from pathlib import Path as _Path

    from .. import gateway as gateway_module

    block = (config or {}).get('identity') or {}
    bot = block.get('bot') or {}
    checks = []
    token_status = str(identity.get('tokenStatus') or '').lower()
    running_app = str(identity.get('appId') or '')
    configured_app = str(bot.get('app_id') or '')
    ok = (not identity.get('error') and identity.get('available') is not False
          and token_status not in ('invalid', 'expired', 'missing'))
    detail = ('mode=bot app_id=%s available=%s tokenStatus=%s'
              % (_mask(running_app) or '未取到', identity.get('available'), identity.get('tokenStatus')))
    if configured_app and running_app and configured_app != running_app:
        ok = False
        detail += '；与配置 app_id=%s 不一致（换应用后要重跑 install.py）' % _mask(configured_app)
    if identity.get('error'):
        ok = False
        detail += '；%s' % str(identity.get('error'))[:120]
    checks.append({'name': 'authenticated', 'status': 'ok' if ok else 'fail', 'detail': detail})

    token_ok, token_detail = False, '取不到 bridge home，无法判 token'
    if home is not None:
        path = gateway_module.token_path(home)
        try:
            bits = stat.S_IMODE(_Path(path).stat().st_mode)
            token_ok = (bits & 0o077) == 0
            token_detail = '%s（%o）' % (path, bits)
        except OSError:
            token_detail = '缺少 %s（重跑 install.py --identity-mode bot，让安装器生成）' % path
    checks.append({'name': 'ingest_token', 'status': 'ok' if token_ok else 'fail',
                   'detail': token_detail})

    try:
        port = int(bot.get('gateway_port') or 8787)
    except (TypeError, ValueError):
        port = 8787
    healthy, health_detail = gateway_module.health(port)
    checks.append({'name': 'ingest_gateway', 'status': 'ok' if healthy else 'fail',
                   'detail': health_detail if healthy else
                             '%s（重跑 install.py --identity-mode bot 让它起回环网关）' % health_detail})
    return checks

def transport_for(config, stop=None, *, default_factory=None):
    """体检/运维入口用：bot 模式返回 BotIdentity，其余返回 ``default_factory()``。

    ``default_factory`` 由调用方给（通常就是既有的 ``Transport`` 包装），这样：
    - 业务代码里不必出现 ``mode == 'bot'``（模式判断只在这里）；
    - 既有测试对 ``Transport`` 的替换点继续有效（user 模式行为一字不变）。
    """
    block = (config or {}).get('identity') or {}
    if str(block.get('mode') or 'user').lower() == 'bot':
        return build_identity(config, stop)
    if default_factory is not None:
        return default_factory()
    return build_identity(config, stop)
