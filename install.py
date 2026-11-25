"""Install only this owned bridge; never copies credentials or changes global Agent settings."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time

from bridge import host, release as brelease

LABEL = 'io.feishu-agent.bridge'
#: 已退役的引擎 A（旧 `bridge/consumer.py`）的 LaunchAgent。只用于**安装时清理**：
#: 它指向的 `feishu-bridge consumer foreground` 已经被删除，留着会让 launchd 每次登录启动失败。
RETIRED_CONSUMER_LABEL = 'io.feishu-agent.cursor-consumer'
# agentkit 并行入口：单独一个 LaunchAgent（默认只写不加载 —— 注册与生效分开）。
AGENTKIT_LABEL = 'io.feishu-agent.agentkit-consumer'
#: 注意力巡检 LaunchAgent（文档里手工装，不由 install.py 写）。列在这里是为了**升级清理**时
#: 不把它当"旧名字"删掉。
ATTENTION_LABEL = 'io.feishu-agent.attention-watch'
#: DSH 常驻消费者（`--with-dsh-consumer` 写的 LaunchAgent）。Windows 的等价物是
#: `dist/wsl2/wsl/install-dsh-consumer.sh` 写的系统级 unit：两端跑**同一份**
#: `current/dsh-consumer.py`，都靠"重载单元"才吃到新 release（只重启进程不算）。
DSH_CONSUMER_LABEL = 'io.feishu-agent.dsh-consumer'
#: 与 install-dsh-consumer.sh 的默认值一致：同一个身份名在同一台机器上只该有一个进程在 claim。
DSH_CONSUMER_DEFAULT = 'dsh-main'
#: 干活端（S3a）：只领 work 事件，在独立工作树里干。与回话端并列、互不抢活。
DSH_WORKER_LABEL = 'io.feishu-agent.dsh-worker'
DSH_WORKER_DEFAULT = 'dsh-worker'
DSH_CONSUMER_KINDS = ('dsh', 'codex')
#: 旧版本的 LaunchAgent/systemd unit 名字里都带 "feishu"，而新名字的前缀是 io.feishu-agent.*。
#: 升级时必须把**任何**带 feishu 的旧 label 停掉删掉：留着它会和新的抢同一个 home
#: （双采集 / 重复回复），而它的名字里还留着上一个人的姓名拼音。
#: 当前版本的 label 白名单。**新增单元必须加进来**：不在里面的会被 `_remove_legacy_services`
#: 当成旧命名删掉（S3a 的干活端单元第一版就被自己删了，测试当场抓到的）。
INGEST_GATEWAY_LABEL = 'io.feishu-agent.ingest-gateway'
#: sidecar 默认往 8787 送事件；这个数与 `bridge/identity/bot.py` 的默认值必须一致。
INGEST_GATEWAY_PORT = 8787

CURRENT_LABELS = (LABEL, AGENTKIT_LABEL, ATTENTION_LABEL, DSH_CONSUMER_LABEL,
                  DSH_WORKER_LABEL, host.IDENTITY_SIDECAR_LABEL, INGEST_GATEWAY_LABEL)
AGENTKIT_DIR = 'agentkit'
#: 消费者运行时：**每个 release 都自带**，不依赖仓库工作树。
#: 理由（2026-11-03 统一）：macOS 的消费者一直从 `releases/<id>/` 跑，而 Windows/WSL2 的
#: systemd unit 指向仓库树，于是"升级后到底跑的哪份代码"两端说法不同。现在两端都从
#: `releases/<id>/` 跑（Windows 用 `current/` 软链），release 才是唯一可运行副本。
#: 发布副本的根级消费者模块：清单只在 `bridge/release.py` 定义一次（指纹与复制共用）。
CONSUMER_FILES = brelease.CONSUMER_FILES
#: 身份 sidecar（as_bot 的长连接收发器）单元名；**一台机器一种身份**，user 模式下必须不存在。
IDENTITY_SIDECAR_LABEL = host.IDENTITY_SIDECAR_LABEL
#: sidecar 的凭据文件与启动包装器（都在 bridge home 下，0600/0700）
IDENTITY_SIDECAR_ENV = 'identity-sidecar.env'
IDENTITY_SIDECAR_BIN = 'identity-sidecar'
#: sidecar 源码目录（随 release 复制，运行时指向 current/sidecar/index.js）
IDENTITY_SIDECAR_DIR = 'sidecar'

#: release 里"直接被 launchd/systemd exec"的入口脚本后缀；其余模块是 0600。
EXECUTABLE_ENTRY_SUFFIXES = ('-consumer.py', '-worker.py')
#: agentkit 是可选层（`--with-agentkit` 才装）：入口 + 适配器包。
AGENTKIT_FILES = ('agentkit-consumer.py', 'agentkit-consumerctl.py')
#: adapter → bridge 侧的 consumer kind（--kind 的白名单）
#: 刻意不含 'fake'：fake 适配器只用于 `python3 -m agentkit` 的平台自检，
#: 把它装成常驻消费者会让假答案去 claim 真实飞书消息。
AGENTKIT_KINDS = {'cursor': 'other', 'dsh': 'dsh', 'codex': 'codex'}
#: 与 `agentkit/rollout.py` 的 PARKED_DIR 一致：被停用的 consumer plist 放这里。
#: 升级时**不能再写回** LaunchAgents —— 那会把用户主动停用的 consumer 重新变成登录自启
#: （两个 consumer 同时 claim 是这条流水线最怕的状态）。
PARKED_DIR = 'disabled-consumers'
# 这里**不允许**出现任何人的 open_id（哪怕是"只用于识别旧安装"）：身份只能来自本机登录
# 的飞书账号，写死一个 open_id 就是把某个人钉进代码 —— 静默默认正是「新机器装完却以别人
# 身份回复」的根因（见 docs/HISTORY.md §4）。旧安装的 config 里已存着自己的值，不需要
# 代码再认识某一个具体的人。
TARGET_OPEN_ID_HELP = (
    '安装必须显式指定你自己的飞书 open_id，不能沿用仓库作者的历史默认值。'
    '取值：lark-cli auth status --json 输出里的 identities.user.openId（ou_ 开头）；'
    '首次安装用 TARGET_OPEN_ID=ou_xxx 传入，之后会写进 config.json，升级时自动沿用。'
)
CONFIG_DEFAULTS = {
    'app_daily_reply_cap': 20,
    'artifact_retention_days': 14,
    # 采集节奏：搜索接口本身 0.6~1.7 秒一次，实测 1 秒间隔连发 10 次不会限流。
    # 真限流时服务端用 230020 明说，transport 会冻结 60 秒——权威信号在服务端，不在这里猜。
    'live_interval_seconds': 3,
    'self_scan_seconds': 10,
    'search_min_interval_seconds': 1.0,
    'backfill_interval_seconds': 60,
    'collector_backoff_seconds': 10,
}
# 源码树内的审查副本目录：在里面跑 install.py 会把 current 回退到修复前代码（P2-20/D12）。
REVIEW_COPY_PARTS = ('coordination', 'review-snapshot', 'review-repaired')
# 启动器必须把 main() 的返回值当进程退出码：`doctor --assert`、`consumer claim` 之类
# 全靠退出码自动化。曾经这里直接 `main(str(home))`，返回值被丢掉 → 所有断言恒为 0。
LAUNCHER_TEMPLATE = ('%s\nimport sys\nfrom pathlib import Path\nhome=Path(%r)\n'
                     'sys.path.insert(0,str(home/"current"))\nfrom bridge.cli import main\n'
                     'raise SystemExit(main(str(home)))\n')


def _atomic_write(path, data, mode=0o600):
    """同目录唯一临时文件 + os.replace：任何中途失败都不会留下半截目标文件（P2-21）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix='.%s.' % path.name, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_json(path, value):
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'), 0o600)


@contextlib.contextmanager
def _flock(path, message):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(message)
        yield
    finally:
        handle.close()


def _sha256(path):
    return brelease.sha256(path)


def _bridge_files(source):
    return brelease.bridge_files(source)


def _file_hashes(paths):
    return brelease.file_hashes(paths)


def _release_digest(source):
    return brelease.release_digest(source)


def _release_id(source):
    return brelease.release_id(source)


RELEASE_NAME = re.compile(r'[0-9a-f]{16}\Z')
# 保留最近几个 release（0 关闭）：旧 release 只是构建产物，但回滚需要最近几代。
SYSTEM_UNIT_DIR = '/etc/systemd/system'
RELEASE_KEEP_DEFAULT = 10


def _referenced_releases(home):
    """被任何已装服务定义引用的 release id（agentkit/consumer 的 plist 是绝对路径，
    删掉它指向的 release 等于让那个 job 下次重启直接跑飞）。"""
    home = Path(home)
    pattern = re.compile(r'releases/([0-9a-f]{16})')
    referenced = set()
    try:
        entries = list(Path(host.service_dir()).iterdir())
    except OSError:
        return referenced
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(errors='replace')
        except OSError:
            continue
        referenced.update(match.group(1) for match in pattern.finditer(text))
    return referenced


def _prune_releases(home, current_id):
    """只删 home/releases 下本工具命名的旧 release；当前 release 与最近 N 个永不删。
    被已装服务定义引用的 release 也不删。"""
    try:
        keep = int(os.environ.get('FEISHU_RELEASE_KEEP', RELEASE_KEEP_DEFAULT))
    except ValueError:
        keep = RELEASE_KEEP_DEFAULT
    releases = Path(home) / 'releases'
    if keep <= 0 or not releases.is_dir():
        return []
    entries = [path for path in releases.iterdir()
               if path.is_dir() and not path.is_symlink() and RELEASE_NAME.match(path.name)]
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    keep_names = {current_id} | {path.name for path in entries[:keep]} | _referenced_releases(home)
    removed = []
    for path in entries:
        if path.name in keep_names:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return sorted(removed)


def _guard_source(source):
    parts = set(Path(source).resolve().parts)
    if parts.intersection(REVIEW_COPY_PARTS):
        raise RuntimeError('Refusing to install from a review copy; run install.py from the canonical source')


def _verify_release(source, release):
    """校验（必要时从零重建）releases/<id>/bridge，返回 bridge 文件名→sha256。

    P2-20：目录存在不再等于内容完整；缺失/不一致一律 rmtree 后重拷，拷完再校验一次，
    失败清理半成品并抛错 —— 绝不把半成品 release 交给 current。
    """
    source = Path(source)
    release = Path(release)
    expected = _file_hashes(_bridge_files(source))
    if not expected:
        raise RuntimeError('Source bridge directory has no Python files: %s' % (source / 'bridge'))
    bridge = release / 'bridge'
    if bridge.is_dir() and not bridge.is_symlink():
        existing = sorted(bridge.glob('*.py'))
        # release 必须是不变副本：符号链接目录/文件一律重建，不能被“哈希一致”蒙混过关。
        if all(path.is_file() and not path.is_symlink() for path in existing) \
                and _file_hashes(existing) == expected:
            return expected
    if bridge.is_symlink() or bridge.is_file():
        bridge.unlink()
    elif bridge.exists():
        shutil.rmtree(bridge)
    if release.exists() and not release.is_dir():
        release.unlink()
    release.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source / 'bridge', bridge, ignore=shutil.ignore_patterns('__pycache__'))
        copied = _file_hashes(sorted(bridge.glob('*.py')))
        if copied != expected:
            missing = sorted(set(expected) - set(copied))
            raise RuntimeError('Release copy verification failed (missing=%s); refusing to promote %s'
                               % (missing, release))
    except BaseException:
        shutil.rmtree(bridge, ignore_errors=True)
        raise
    return expected


def _merge_config_defaults(config):
    """新增默认值但不覆盖已有键（升级不能重置运维者的既有配置）。"""
    merged = dict(config or {})
    for key, value in CONFIG_DEFAULTS.items():
        merged.setdefault(key, value)
    return merged


def _read_config(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError('Existing config.json is unreadable or not valid JSON: %s' % error)
    if not isinstance(value, dict):
        raise RuntimeError('Existing config.json must contain a JSON object')
    return value


def _launcher_script(home):
    return LAUNCHER_TEMPLATE % (host.launcher_shebang(sys.executable), str(home))


def resolve_target_open_id(config=None, env=None):
    """解析「这台机器的主人是哪个飞书账号」，绝不回落到仓库作者的历史默认值。

    优先级：本次安装的环境变量 → 既有 config.json（升级时沿用）→ 报错。
    旧安装如果存的正是历史默认值，只提示不阻断——阻断会让作者本人现网的升级失败，
    而不阻断也不会把该值再扩散到新机器（它不再是任何默认值）。
    """
    env = os.environ if env is None else env
    existing = (config or {}).get('target_open_id')
    target = env.get('TARGET_OPEN_ID') or existing
    if not target or not str(target).strip():
        raise RuntimeError(TARGET_OPEN_ID_HELP)
    target = str(target).strip()
    if not target.startswith('ou_'):
        raise RuntimeError('TARGET_OPEN_ID 应以 ou_ 开头（飞书 open_id），收到：%s\n%s'
                           % (target, TARGET_OPEN_ID_HELP))
    return target


SIGNATURE_MAX = 60
SIGNATURE_HELP = ('回复正文末尾的署名由安装者决定：用 --signature 或环境变量 SIGNATURE 传入；'
                  '不传则沿用既有 config.json，仍没有就使用内置默认。')


def resolve_signature(config=None, env=None):
    """署名由安装者决定：本次显式给 → 既有配置 → None（沿用内置默认）。

    只做最朴素的清洗（折叠空白、限长），不做任何"聪明"的改写；安装者写什么就是什么。
    """
    env = os.environ if env is None else env
    value = env.get('SIGNATURE')
    if value is None:
        value = (config or {}).get('signature')
    if value is None:
        return None
    text = ' '.join(str(value).split())
    if not text:
        return None
    if len(text) > SIGNATURE_MAX:
        raise RuntimeError('署名太长（%d > %d）：%s' % (len(text), SIGNATURE_MAX, SIGNATURE_HELP))
    return text


def agentkit_sources(source):
    """列出 agentkit 打包缺了哪些文件（空列表 = 完整）。"""
    source = Path(source)
    missing = [name for name in AGENTKIT_FILES + CONSUMER_FILES if not (source / name).is_file()]
    if not (source / AGENTKIT_DIR / 'adapters' / 'legacy.py').is_file():
        missing.append(AGENTKIT_DIR + '/adapters/legacy.py')
    return missing


def _copy_consumer(source, release):
    """把消费者运行时复制进 release（内容不同才写，返回 文件名→sha256）。

    与 `_copy_agentkit` 同一套写法：release 是不变副本，写之前不删目录、只原子覆盖单个文件；
    入口脚本给 0700，模块 0600。
    """
    source, release = Path(source), Path(release)
    hashes = {}
    for name in CONSUMER_FILES:
        data = (source / name).read_bytes()
        target = Path(release) / name
        # 入口脚本必须可执行：launchd 直接 exec 它（不经过 python 解释器），0600 会得到
        # `last exit code = 78: EX_CONFIG`，表现为"单元装了、进程起不来、日志还是空的"。
        # 2026-11-19 部署现场就是踩在这条上（只认了 `-consumer.py`，漏了 `-worker.py`）。
        mode = 0o700 if name.endswith(EXECUTABLE_ENTRY_SUFFIXES) else 0o600
        if not (target.is_file() and not target.is_symlink() and target.read_bytes() == data):
            _atomic_write(target, data, mode)
        elif stat.S_IMODE(target.stat().st_mode) != mode:
            os.chmod(str(target), mode)   # 内容一样但权限不对：只修权限，不重写文件
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes


def _copy_identity_sidecar(source, release):
    """把 ``sidecar/``（Node 长连接桥）复制进 release。

    为什么随 release 走：sidecar 是**运行时的一部分**（as_bot 的收消息通道），
    不能指望运维机器上另有一份仓库副本；换 release 时它必须跟着换。
    """
    source_dir = Path(source) / IDENTITY_SIDECAR_DIR
    if not source_dir.is_dir():
        return {}
    target_dir = Path(release) / IDENTITY_SIDECAR_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for item in sorted(source_dir.rglob('*')):
        if not item.is_file() or item.name == 'node_modules' or '__pycache__' in item.parts:
            continue
        relative = item.relative_to(source_dir)
        target = target_dir / relative
        data = item.read_bytes()
        mode = 0o700 if relative == Path('index.js') else 0o600
        if not (target.is_file() and target.read_bytes() == data):
            _atomic_write(target, data, mode)
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    return hashes



# ---------------------------------------------------------------------------
# 身份（as_user / as_bot）：一台机器一种身份，安装按模式只装一套
# ---------------------------------------------------------------------------
TOKEN_FILENAME = 'gateway-token'


def identity_config(config, *, mode=None, owner_open_ids=(), bot_app_id=None,
                    bot_open_id=None, bot_app_secret_file=None):
    """把命令行/既有配置合成 ``identity`` 块（纯计算，不写盘）。

    **一台机器一种身份**：这里只做"形状"合成，矛盾判定在 :func:`identity_validate`，
    两者都在任何写操作之前跑完（契约：preflight 先于写盘）。
    """
    block = dict(((config or {}).get('identity') or {}))
    if mode:
        block['mode'] = str(mode).strip().lower()
    block.setdefault('mode', 'user')
    owners = [item for item in (block.get('owner_open_ids') or []) if item]
    for item in owner_open_ids or ():
        if item and item not in owners:
            owners.append(item)
    if owners:
        block['owner_open_ids'] = owners
    bot = dict(block.get('bot') or {})
    for key, value in (('app_id', bot_app_id), ('bot_open_id', bot_open_id),
                       ('app_secret_file', bot_app_secret_file)):
        if value:
            bot[key] = value
    if bot:
        block['bot'] = bot
    return block


def identity_validate(block, *, target_open_id=None):
    """校验 ``identity`` 块；返回 ``(mode, 给运维看的提示列表)``，不合规直接抛错。

    错误文案必须能直接照着做：这两种失败（没配 app_id / 拿不到 app secret）是 as_bot
    安装时最常见的两处卡点。
    """
    mode = (block or {}).get('mode')
    if mode not in ('user', 'bot'):
        raise RuntimeError("identity.mode 只能是 'user' 或 'bot'，当前是 %r" % (mode,))
    bot = (block or {}).get('bot') or {}
    notes = []
    if mode == 'user':
        if isinstance(bot, dict) and bot.get('app_id'):
            raise RuntimeError(
                '配置矛盾：identity.mode=user，却配了 identity.bot.app_id=%r。'
                '一台机器只能有一种身份：删掉 identity.bot 整块，或把 identity.mode 改成 bot。'
                % (bot.get('app_id'),))
        return mode, notes
    # bot 模式
    if not bot.get('app_id'):
        raise RuntimeError('as_bot 需要 identity.bot.app_id：用 --bot-app-id <cli_...> 指定'
                           '（复用现有飞书应用即可，不新建也支持）')
    owners = [item for item in (block.get('owner_open_ids') or []) if item]
    if not owners and target_open_id:
        owners = [target_open_id]
    if not owners:
        raise RuntimeError('as_bot 需要 identity.owner_open_ids（谁能批准、谁能发命令）：'
                           '用 --owner-open-id ou_xxx 指定，可重复')
    secret = bot.get('app_secret')
    secret_file = bot.get('app_secret_file')
    if not secret and not secret_file:
        raise RuntimeError(
            'as_bot 需要应用密钥来建长连接：用 --bot-app-secret-file <0600 文件> 指定；'
            '或先在 lark-cli 侧执行 `lark-cli config keychain-downgrade` 让密钥落到本地文件后重试')
    if not bot.get('bot_open_id'):
        notes.append('未配置 identity.bot.bot_open_id：@ 判定会退化成"有人被 @"（偏保守，不静默）')
    return mode, notes


def _sidecar_secret(block):
    """读出 sidecar 需要的 app secret（只进内存/0600 文件，绝不打印）。"""
    bot = (block or {}).get('bot') or {}
    if bot.get('app_secret'):
        return str(bot['app_secret'])
    path = Path(str(bot.get('app_secret_file'))).expanduser()
    if not path.is_file():
        raise RuntimeError('identity.bot.app_secret_file 不存在：%s' % path)
    return path.read_text(encoding='utf-8').strip()


#: sidecar 的依赖落在这（**持久**，跨 release 复用）：ESM 的 import 只从脚本所在目录往上找
#: node_modules，所以应用与依赖必须同目录；放进 release 会破坏"release 是不变副本"。
IDENTITY_SIDECAR_APP = 'identity-sidecar-app'


def ensure_sidecar_deps(home, node_binary, npm_binary=None):
    """把 sidecar 的 app 与依赖准备好（幂等）：返回可执行入口的绝对路径。

    为什么安装器必须做这一步：sidecar 依赖 `@larksuiteoapi/node-sdk`，没装依赖时进程
    一启动就崩——那正是"装了不启动"的老毛病。判据是**依赖目录真的存在**，不是"单元写了"。
    """
    home = Path(home)
    source = home / 'current' / IDENTITY_SIDECAR_DIR
    if not (source / 'index.js').is_file():
        raise RuntimeError('release 里没有 sidecar/index.js：安装包不完整（先跑 install.py 构建 release）')
    app = home / IDENTITY_SIDECAR_APP
    app.mkdir(parents=True, exist_ok=True)
    entry = app / 'index.js'
    payload = (source / 'index.js').read_bytes()
    if not (entry.is_file() and entry.read_bytes() == payload):
        _atomic_write(entry, payload, 0o700)
    manifest = source / 'package.json'
    target_manifest = app / 'package.json'
    manifest_bytes = manifest.read_bytes() if manifest.is_file() else b'{}'
    manifest_changed = not (target_manifest.is_file() and target_manifest.read_bytes() == manifest_bytes)
    if manifest_changed:
        _atomic_write(target_manifest, manifest_bytes, 0o600)
    sdk = app / 'node_modules' / '@larksuiteoapi' / 'node-sdk'
    if sdk.is_dir() and not manifest_changed:
        return entry
    npm = npm_binary or shutil.which('npm')
    if not npm:
        raise RuntimeError('as_bot 需要 npm 装 sidecar 依赖（@larksuiteoapi/node-sdk）：'
                           '把 npm 放进 PATH，或先在 %s 里手工 npm install --omit=dev' % app)
    done = subprocess.run([npm, 'install', '--omit=dev', '--no-audit', '--no-fund', '--loglevel=error'],
                          cwd=str(app), capture_output=True, text=True, timeout=600)
    if done.returncode != 0 or not sdk.is_dir():
        tail = ' '.join(((done.stderr or done.stdout or '').strip().splitlines() or [''])[-3:])[:300]
        raise RuntimeError('sidecar 依赖安装失败（npm install 退出码 %s）：%s\n'
                           '下一步：在 %s 里手工跑 npm install --omit=dev 看具体报错'
                           % (done.returncode, tail, app))
    return entry


def _sidecar_runtime_assets(home, block, *, node_binary, npm_binary=None):
    """写 sidecar 的 0600 环境文件与 0700 包装脚本，返回 (env 路径, 包装器路径)。"""
    home = Path(home)
    bot = (block or {}).get('bot') or {}
    script = ensure_sidecar_deps(home, node_binary, npm_binary=npm_binary)
    token_file = home / TOKEN_FILENAME
    lines = ['FEISHU_APP_ID=%s' % bot.get('app_id'),
             'FEISHU_APP_SECRET=%s' % _sidecar_secret(block),
             'FEISHU_INGEST_URL=%s' % (bot.get('ingest_url')
                                       or 'http://127.0.0.1:%s/v1/ingest' % (bot.get('gateway_port') or 8787)),
             'FEISHU_INGEST_TOKEN_FILE=%s' % token_file,
             'FEISHU_LOG_LEVEL=%s' % (bot.get('log_level') or 'info')]
    env_path = home / IDENTITY_SIDECAR_ENV
    _atomic_write(env_path, ('\n'.join(lines) + '\n').encode('utf-8'), 0o600)
    wrapper = home / 'bin' / IDENTITY_SIDECAR_BIN
    script_text = ('#!/bin/sh\nset -eu\nset -a\n. "%s"\nset +a\nexec "%s" "%s"\n'
                   % (env_path, node_binary, script))
    _atomic_write(wrapper, script_text.encode('utf-8'), 0o700)
    return env_path, wrapper


def _identity_sidecar_service(home, wrapper):
    """sidecar 的服务定义：与 bridge 同一套后端（launchd / systemd 用户单元）。"""
    return host.service_definition(home, Path(wrapper), label=IDENTITY_SIDECAR_LABEL,
                                   args=(), log_prefix='identity-sidecar')


def install_identity_sidecar(home, block):
    """bot 模式：写 sidecar 资产 + 单元并让它跑起来；返回回执 dict。"""
    home = Path(home)
    node_binary = os.environ.get('NODE_BINARY') or shutil.which('node')
    if not node_binary:
        raise RuntimeError('as_bot 需要 node（sidecar 长连接）：把 node 放进 PATH 或设 NODE_BINARY')
    env_path, wrapper = _sidecar_runtime_assets(home, block, node_binary=node_binary)
    unit_path = Path(host.service_path(IDENTITY_SIDECAR_LABEL))
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    definition = _identity_sidecar_service(home, wrapper)
    payload = plistlib.dumps(definition) if host.is_macos() else definition.encode('utf-8')
    _atomic_write(unit_path, payload, 0o600)
    if not _service_dir_is_real(unit_path.parent):
        # 测试/自定义安装：只写文件，不 reload（否则会用临时 plist 顶掉真机上的同名单元）。
        return {'label': IDENTITY_SIDECAR_LABEL, 'unit': str(unit_path), 'env': str(env_path),
                'wrapper': str(wrapper), 'reloaded': False,
                'skipped': '临时单元目录：只写文件、不 reload'}
    reloaded = host.reload_service(IDENTITY_SIDECAR_LABEL, unit_path)
    return {'label': IDENTITY_SIDECAR_LABEL, 'unit': str(unit_path), 'env': str(env_path),
            'wrapper': str(wrapper), 'reloaded': bool(reloaded.get('ok')),
            'pid': reloaded.get('pid'), 'error': reloaded.get('error')}


def retire_identity_sidecar(home):
    """user 模式：把可能存在的 sidecar 单元/资产清掉（幂等；没有就返回 None）。

    为什么必须清：一台机器一种身份。留着 sidecar 等于 bot 通道还在收事件——那正是
    "两个身份一起开"的事故形态。
    """
    home = Path(home)
    unit_path = Path(host.service_path(IDENTITY_SIDECAR_LABEL))
    removed = []
    real_unit_dir = _service_dir_is_real(unit_path.parent)
    state = host.service_state(IDENTITY_SIDECAR_LABEL) if real_unit_dir else {'loaded': False}
    if state.get('loaded'):
        if host.is_macos():
            subprocess.run(['/bin/launchctl', 'bootout', 'gui/%s/%s' % (os.getuid(), IDENTITY_SIDECAR_LABEL)],
                           capture_output=True, text=True, timeout=60)
        else:
            subprocess.run(['systemctl', '--user', 'disable', '--now', IDENTITY_SIDECAR_LABEL],
                           capture_output=True, text=True, timeout=60)
        removed.append('unit-stopped')
    for path in (unit_path, home / IDENTITY_SIDECAR_ENV, home / 'bin' / IDENTITY_SIDECAR_BIN):
        if path.exists() or path.is_symlink():
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
    if real_unit_dir and state.get('loaded') and host.is_macos():
        subprocess.run(['/bin/launchctl', 'kickstart', '-k', 'gui/%s/%s' % (os.getuid(), LABEL)],
                       capture_output=True, text=True, timeout=60)
    return {'label': IDENTITY_SIDECAR_LABEL, 'removed': removed} if removed else None


def _ingest_port(block):
    """配置里的回环 ingest 端口；非法值直接报错（不猜）。"""
    bot = (block or {}).get('bot') or {}
    raw = bot.get('gateway_port', INGEST_GATEWAY_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError('identity.bot.gateway_port 必须是整数（当前 %r）' % (raw,)) from None
    if not 1 <= port <= 65535:
        raise RuntimeError('identity.bot.gateway_port 必须在 1..65535（当前 %r）' % (raw,))
    return port


def install_ingest_gateway(home, block, *, launcher=None):
    """bot 模式：生成共享 token + 起回环 ingest 网关（幂等）。

    为什么安装器必须做这两件：sidecar 启动后第一件事就是读 ``<home>/gateway-token``，
    再把事件 ``POST 127.0.0.1:<port>/v1/ingest``。两者缺一个的表现都是"机器人不回话"——
    2026-11-21 真机就是手工补的：token ENOENT、且 sidecar 默认 8787 与网关默认 8766 不一致。
    """
    from bridge import gateway as gateway_module

    home = Path(home)
    _, token_file, created = gateway_module.generate_token(home)
    port = _ingest_port(block)
    launcher = Path(launcher) if launcher else home / 'bin' / 'feishu-bridge'
    unit_path = Path(host.service_path(INGEST_GATEWAY_LABEL))
    definition = host.service_definition(
        home, launcher, label=INGEST_GATEWAY_LABEL,
        args=('events', 'serve', '--host', '127.0.0.1', '--port', str(port)),
        log_prefix='ingest-gateway')
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.dumps(definition) if host.is_macos() else definition.encode('utf-8')
    _atomic_write(unit_path, payload, 0o600)
    info = {'label': INGEST_GATEWAY_LABEL, 'unit': str(unit_path), 'port': port,
            'token': str(token_file), 'token_created': created}
    if not _service_dir_is_real(unit_path.parent):
        # 测试/自定义安装：只写文件，不 reload（否则会用临时单元顶掉真机上的同名单元）。
        info.update({'reloaded': False, 'health': False,
                     'skipped': '临时单元目录：只写文件、不 reload'})
        return info
    reloaded = host.reload_service(INGEST_GATEWAY_LABEL, unit_path)
    healthy, health_detail = gateway_module.health(port)
    info.update({'reloaded': bool(reloaded.get('ok')), 'pid': reloaded.get('pid'),
                 'error': reloaded.get('error'), 'health': healthy,
                 'health_detail': health_detail})
    if not healthy:
        print('warning: ingest 网关已写 %s，但 %s 没通（%s）——sidecar 的事件会送不进来；'
              '查 `systemctl --user status %s`（或 launchctl print）'
              % (unit_path, '127.0.0.1:%s/health' % port, health_detail, INGEST_GATEWAY_LABEL),
              file=sys.stderr)
    return info


def retire_ingest_gateway(home):
    """user 模式：停掉并删掉 ingest 网关单元（幂等；没有就返回 None）。

    为什么必须清：一台机器一种身份。留着它等于在 user 模式下多开一个只回环的端口，
    而它服务的 sidecar 已经不在了——端口上没人用的东西迟早会被人误以为"可以接着用"。
    """
    home = Path(home)
    unit_path = Path(host.service_path(INGEST_GATEWAY_LABEL))
    real_unit_dir = _service_dir_is_real(unit_path.parent)
    removed = []
    state = host.service_state(INGEST_GATEWAY_LABEL) if real_unit_dir else {'loaded': False}
    if state.get('loaded'):
        if host.is_macos():
            subprocess.run(['/bin/launchctl', 'bootout',
                            'gui/%s/%s' % (os.getuid(), INGEST_GATEWAY_LABEL)],
                           capture_output=True, text=True, timeout=60)
        else:
            subprocess.run(['systemctl', '--user', 'disable', '--now', INGEST_GATEWAY_LABEL],
                           capture_output=True, text=True, timeout=60)
        removed.append('unit-stopped')
    if unit_path.exists() or unit_path.is_symlink():
        try:
            unit_path.unlink()
            removed.append(str(unit_path))
        except OSError:
            pass
    return {'label': INGEST_GATEWAY_LABEL, 'removed': removed} if removed else None


def _copy_agentkit(source, release):
    """把 agentkit 运行时复制进 release 目录（先删后拷，release 必须是不变副本）。"""
    source, release = Path(source), Path(release)
    target = release / AGENTKIT_DIR
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(source / AGENTKIT_DIR, target,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    hashes = {}
    for name in AGENTKIT_FILES:
        data = (source / name).read_bytes()
        _atomic_write(release / name, data,
                      0o700 if name in ('agentkit-consumer.py', 'agentkit-consumerctl.py')
                      else 0o600)
        hashes[name] = hashlib.sha256(data).hexdigest()
    for path in sorted(target.rglob('*.py')):
        hashes[str(path.relative_to(release))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _service_path_keeping_parked(path, home):
    """如果该 plist 已被"停用"过（parked 副本存在），就写到 parked 位置去。"""
    path = Path(path)
    parked = Path(home) / PARKED_DIR / path.name
    return parked if parked.is_file() else path


def _remove_retired_consumer(home, service):
    """把引擎 A 的 LaunchAgent 与 park 副本删掉，返回删掉的路径。

    模型：发现哪一层建错了就拆掉那一层，而不是在上面继续修补——留着这个 plist
    只会在下次登录时反复启动失败，还让后来的人以为"可以恢复成 cursor-main"。
    """
    removed = []
    plist = Path(service).with_name(RETIRED_CONSUMER_LABEL + '.plist')
    if plist.exists():
        if host.is_macos():
            try:
                subprocess.run(['/bin/launchctl', 'bootout',
                                'gui/%s/%s' % (os.getuid(), RETIRED_CONSUMER_LABEL)],
                               capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            plist.unlink()
            removed.append(str(plist))
        except OSError:
            pass
    parked = Path(home) / PARKED_DIR / (RETIRED_CONSUMER_LABEL + '.plist')
    if parked.exists():
        try:
            parked.unlink()
            removed.append(str(parked))
        except OSError:
            pass
    return removed



def _plist_label(path):
    """读 plist 的 Label；坏文件返回 None（不猜）。"""
    try:
        payload = plistlib.loads(Path(path).read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    label = payload.get('Label')
    return label if isinstance(label, str) else None


def _plist_owned_by_bridge(plist, home):
    """plist 的 ProgramArguments 是否指向本安装（不靠 label 名字猜）。

    为什么要看 argv：曾经出现过 `com.<作者拼音>.dsh-consumer` 这种"名字里没有 feishu、
    却分明是本项目的 LaunchAgent"的单元（HISTORY §9.1 的改名遗留）。靠名字判断会漏掉它，
    而它和新的 unit 抢同一个 home（双采集 / 重复回复）。判据只有一条：argv 指向**本次安装的
    home**（不看入口文件名 —— 否则用临时 home 的测试会把真机上的消费者单元删掉）。
    """
    try:
        payload = plistlib.loads(Path(plist).read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    home_text = str(Path(home)) + os.sep
    for argument in payload.get('ProgramArguments') or []:
        if str(argument).startswith(home_text):
            return True
    return False


def _remove_legacy_services(home, service_dir):
    """把旧命名的 LaunchAgent 与 park 副本停掉并删除，返回删掉的路径。

    判据不是"列一张旧名字清单"（那会把上一个人的姓名拼音写回仓库），而是：
    **不是当前 label，且（label/文件名带 feishu 或 argv 指向本安装）** 就是旧版本留下的。
    """
    removed = []
    roots = [Path(service_dir), Path(home) / PARKED_DIR]
    for root in roots:
        if not root.is_dir():
            continue
        for plist in sorted(root.glob('*.plist')):
            label = _plist_label(plist) or plist.stem
            if label in CURRENT_LABELS:
                continue
            if 'feishu' not in label.lower() and not _plist_owned_by_bridge(plist, home):
                continue
            if host.is_macos() and root == Path(service_dir):
                try:
                    subprocess.run(['/bin/launchctl', 'bootout',
                                    'gui/%s/%s' % (os.getuid(), label)],
                                   capture_output=True, text=True, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    pass
            try:
                plist.unlink()
                removed.append(str(plist))
            except OSError:
                pass
    return removed


def _dsh_binary():
    """挑出要写进消费者 PATH 的 dsh 可执行文件；找不到就**在写任何东西之前**失败。

    为什么要挑而不是直接继承 PATH：本机可能同时装着两份（例如 `~/.local/bin/dsh` 与
    `~/.npm-global/bin/dsh`），"装的是哪份、跑的是哪份"必须只有一个答案 —— 消费者的
    PATH 由安装器显式写死，receipt 里记下选中的绝对路径与版本，漂移一眼可见。
    """
    candidates = []
    override = os.environ.get('DSH_BINARY')
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.home() / '.npm-global/bin/dsh')
    found = shutil.which('dsh')
    if found:
        candidates.append(Path(found))
    candidates += [Path.home() / '.local/bin/dsh', Path('/opt/homebrew/bin/dsh'),
                   Path('/usr/local/bin/dsh')]
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit('没找到 DSH CLI：先 `npm install -g @deepseek-ai/dsh`（Node ≥ 20.12），'
                     '或用 DSH_BINARY=<绝对路径> 指定。消费者没有引擎时会在领取事件之前就失败，'
                     '所以安装器不写一个跑不起来的单元。')


def _dsh_version(binary):
    """`dsh --version` 的第一行；拿不到就写 unknown（不编版本号）。"""
    try:
        done = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return 'unknown (%s)' % error
    lines = (done.stdout or done.stderr or '').strip().splitlines()
    return lines[0][:80] if lines else 'unknown'


def _dsh_consumer_definition(home, consumer, kind, workspace, dsh_binary):
    """DSH 消费者的单元定义：ProgramArguments 指向 `current/dsh-consumer.py`。

    指向 `current/`（而不是 `releases/<id>/`）与 Windows 的 install-dsh-consumer.sh 一致：
    切换 current 是唯一提交点，**重载单元**才换代码；这样升级只需要重载，不需要改写单元。
    """
    entry = Path(home) / 'current' / 'dsh-consumer.py'
    definition = host.service_definition(
        home, entry, label=DSH_CONSUMER_LABEL,
        args=('--consumer', consumer, '--kind', kind, '--home', str(home),
              '--workspace', str(workspace)),
        log_prefix='dsh-consumer')
    if host.is_macos() and isinstance(definition, dict):
        environment = definition.setdefault('EnvironmentVariables', {})
        # dsh 从 PATH 找自己（~/.npm-global/bin 常不在 launchd 默认 PATH 里）；
        # DSH_HOME 与 Windows unit 的 EnvironmentFile 是同一个事实：登录态/凭证放哪。
        environment['PATH'] = '%s:%s' % (Path(dsh_binary).parent,
                                         environment.get('PATH') or host.default_path())
        environment.setdefault('DSH_HOME', str(Path.home() / '.dsh'))
    return definition


def _write_dsh_consumer(home, service_dir, consumer, kind, workspace, dsh_binary):
    """写消费者的 LaunchAgent，返回 plist 路径（重载在锁外单独做）。"""
    definition = _dsh_consumer_definition(home, consumer, kind, workspace, dsh_binary)
    path = Path(service_dir) / (DSH_CONSUMER_LABEL + '.plist')
    _atomic_write(path, plistlib.dumps(definition), 0o600)
    return path


def _dsh_worker_definition(home, consumer, dsh_binary):
    """干活端的单元定义：指向 `current/dsh-worker.py`。

    与回话端同一个道理——单元指向 `current/`，**重载单元才换代码**；升级只重载、不改写参数。
    **没有"在哪个仓库、工作树放哪"**：干活端不假设机器上有 git（主人 2026-11-19 定），
    每个活用自己的独立目录（`<bridge home>/work/<task_id>/space/`）。
    """
    entry = Path(home) / 'current' / 'dsh-worker.py'
    definition = host.service_definition(
        home, entry, label=DSH_WORKER_LABEL,
        args=('--consumer', consumer, '--home', str(home)),
        log_prefix='dsh-worker')
    if host.is_macos() and isinstance(definition, dict):
        environment = definition.setdefault('EnvironmentVariables', {})
        environment['PATH'] = '%s:%s' % (Path(dsh_binary).parent,
                                         environment.get('PATH') or host.default_path())
        environment.setdefault('DSH_HOME', str(Path.home() / '.dsh'))
    return definition


def _write_dsh_worker(home, service_dir, consumer, dsh_binary):
    definition = _dsh_worker_definition(home, consumer, dsh_binary)
    path = Path(service_dir) / (DSH_WORKER_LABEL + '.plist')
    _atomic_write(path, plistlib.dumps(definition), 0o600)
    return path


def _reload_dsh_worker(home, plist, consumer, dsh_binary):
    """重载干活端单元，并等它真的注册/领过活（同回话端的等待逻辑）。"""
    if not _service_dir_is_real(Path(plist).parent):
        return {'label': DSH_WORKER_LABEL, 'plist': str(plist), 'consumer': consumer,
                'dsh_binary': str(dsh_binary), 'reloaded': False,
                'skipped': '临时单元目录：只写文件、不 reload'}
    was_loaded = bool(host.service_state(DSH_WORKER_LABEL).get('loaded'))
    reloaded = host.reload_service(DSH_WORKER_LABEL, plist)
    live = _wait_consumer_live(home, consumer) if reloaded.get('ok') else False
    info = {'label': DSH_WORKER_LABEL, 'plist': str(plist), 'consumer': consumer,
            'dsh_binary': str(dsh_binary), 'was_loaded': was_loaded,
            'reloaded': bool(reloaded.get('ok')), 'pid': reloaded.get('pid'),
            'live': live, 'error': reloaded.get('error')}
    if not live:
        print('error: 干活端单元已写入 %s，但 %s 秒内没看到它登记（reload=%s，pid=%s，%s）。\n'
              '       多半是 workbench 自检没过（dsh 起不来 / 没登录 / 工作目录不可写）：'
              '跑 python3 %s --check 看具体原因。'
              % (plist, 45, info['reloaded'], info['pid'], info['error'] or '无报错',
                 Path(home) / 'current/dsh-worker.py'), file=sys.stderr)
    return info


def _service_dir_is_real(service_dir):
    """这个单元目录是不是"真机的 LaunchAgents/systemd 目录"。

    为什么要有这道闸：`launchctl` 的 label 是**全局**的，与 plist 放在哪无关。测试和自定义
    `--plist` 安装会把单元写进临时目录，但如果照常 reload，就会用临时 plist 顶掉真机上的同名 job
    ——2026-11-19 部署后干活端反复 `EX_CONFIG`(78) 就是这么来的：真 job 被一份已删除的临时
    plist 顶掉，`runs=5`、日志却是空的。临时目录里的安装**只校验文件，不碰真机服务**。
    """
    try:
        return Path(service_dir).resolve() == Path(host.service_path()).parent.resolve()
    except OSError:
        return False


def _ensure_dsh_worker(home, service_dir, source):
    """升级路径：干活端单元**缺了就补写**，存在就原样重载。

    为什么要有"补写"这一支：S3a 之前的安装从来没有这份单元，`install.py --verify` 走的是升级路径
    （`dsh is None`），过去只重载、不补写 —— 结果是**升级上来的机器永远没有干活端，而且没有任何提示**。
    批准之后没人干活，看起来像"活卡在 running"，其实根本没装。静默降级不是修复。
    """
    path = Path(service_dir) / (DSH_WORKER_LABEL + '.plist')
    if path.is_file():
        return _reload_existing_dsh_worker(home, service_dir)
    if not _service_dir_is_real(service_dir):
        # 测试/自定义安装：只写文件，不碰真机 launchd。
        plist = _write_dsh_worker(home, service_dir, DSH_WORKER_DEFAULT, _dsh_binary_or_none())
        return {'label': DSH_WORKER_LABEL, 'plist': str(plist), 'reloaded': False,
                'skipped': '临时单元目录：只写文件、不 reload'}
    plist = _write_dsh_worker(home, service_dir, DSH_WORKER_DEFAULT, _dsh_binary_or_none())
    return _reload_dsh_worker(home, plist, DSH_WORKER_DEFAULT, _dsh_binary_or_none())


def _reload_existing_dsh_worker(home, service_dir):
    """升级时如果这台机器已经装了干活端，也要让它吃到新 release（与回话端同一条规矩）。"""
    path = Path(service_dir) / (DSH_WORKER_LABEL + '.plist')
    if not path.is_file():
        return None
    if not _service_dir_is_real(service_dir):
        return {'label': DSH_WORKER_LABEL, 'plist': str(path), 'reloaded': False,
                'skipped': '临时单元目录：只读校验、不 reload'}
    try:
        payload = plistlib.loads(path.read_bytes())
        argv = list(payload['ProgramArguments'])
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        return {'label': DSH_WORKER_LABEL, 'reloaded': False,
                'error': '单元文件读不出来，没重载'}
    def value(flag, default):
        return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default
    consumer = value('--consumer', DSH_WORKER_DEFAULT)
    legacy = [flag for flag in ('--repo', '--worktree-root') if flag in argv]
    if legacy:
        # 旧单元里带着已经删掉的参数：新的 dsh-worker.py 会以"无法识别的参数"直接退出。
        # 所以这里不是"原样重载"，而是按新形状重写单元——否则升级完干活端就再也起不来了。
        plist = _write_dsh_worker(home, service_dir, consumer, _dsh_binary_or_none())
        return _reload_dsh_worker(home, plist, consumer, _dsh_binary_or_none())
    return _reload_dsh_worker(home, path, consumer, _dsh_binary_or_none())


def _dsh_binary_or_none():
    try:
        return _dsh_binary()
    except SystemExit:
        return 'dsh'


def _wait_consumer_live(home, consumer, timeout=45, fresh_seconds=45):
    """等消费者**真的领过活**（consumers.last_seen_at 是新鲜的），而不是只看 launchd 有 pid。

    pid 只证明进程起来了；没有凭证时会立刻退出、launchd 反复重拉，那种"看起来在跑"的
    状态必须暴露成 live=False，否则下一条 @ 又是没人答。
    """
    from bridge.store import Store
    deadline = time.time() + max(0, timeout)
    while True:
        store = None
        try:
            store = Store(str(Path(home) / 'state.sqlite3'))
            for row in store.consumers():
                if row.get('name') == consumer and time.time() - (row.get('last_seen_at') or 0) < fresh_seconds:
                    return True
        except (OSError, RuntimeError, sqlite3.Error):
            pass
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def _existing_dsh_consumer(service_dir):
    """读出已安装的消费者单元身份（没有就返回 None）：升级时必须让它跟着换 release。

    判据不靠"有没有传 --with-dsh-consumer"：单元存在就说明这台机器在常驻消费事件，
    只升 bridge 不重载它 = 回复链路继续跑旧代码（2026-11-04 审计与 HISTORY §9 都记过）。
    """
    path = Path(service_dir) / (DSH_CONSUMER_LABEL + '.plist')
    if not path.is_file():
        return None
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    arguments = [str(item) for item in payload.get('ProgramArguments') or []]
    consumer = 'dsh-main'
    for index, item in enumerate(arguments[:-1]):
        if item == '--consumer':
            consumer = arguments[index + 1]
            break
    return {'plist': path, 'consumer': consumer, 'kind': 'dsh'}


def _reload_existing_dsh_consumer(home, service_dir):
    """升级路径：不加 --with-dsh-consumer 也要把已有消费者重载到新 release。"""
    existing = _existing_dsh_consumer(service_dir)
    if existing is None:
        return None
    was_loaded = bool(host.service_state(DSH_CONSUMER_LABEL).get('loaded'))
    reloaded = host.reload_service(DSH_CONSUMER_LABEL, existing['plist'])
    live = _wait_consumer_live(home, existing['consumer']) if reloaded.get('ok') else False
    info = {'label': DSH_CONSUMER_LABEL, 'plist': str(existing['plist']),
            'consumer': existing['consumer'], 'kind': existing['kind'], 'created': False,
            'was_loaded': was_loaded, 'reloaded': bool(reloaded.get('ok')),
            'pid': reloaded.get('pid'), 'live': live, 'error': reloaded.get('error')}
    if not live:
        print('error: 已安装的消费者单元 %s 没有重载成功（%s，pid=%s）：它还在跑旧 release。\n'
              '       下一步：python3 install.py --with-dsh-consumer（重建单元并重启），'
              '或手工 launchctl kickstart -k gui/%d/%s 后看 feishu-bridge consumer status。'
              % (existing['plist'], info['error'] or '无报错', info['pid'], os.getuid(), DSH_CONSUMER_LABEL),
              file=sys.stderr)
    return info


def _system_unit_for(home, launcher):
    """WSL2/Linux：找出**正在托管本安装**的系统级 unit，按 ExecStart 指向本安装判断。

    为什么不只看 `io.feishu-agent.bridge.service`：11-04 之前的安装用的是带人名拼音的旧名
    （`com.<人名>.feishu-personal-agent.service`），只认新名字会得出"没有系统级 unit"的错误结论，
    于是升级时又写一份用户级 unit 并把它 enable/start —— 两个 bridge 抢同一个 home
    （双采集 / 重复回复，HISTORY §7）。判据只看 ExecStart 里的 launcher 绝对路径：
    它唯一地指向本次安装的 home，与单元叫什么名字无关。
    """
    directory = Path(SYSTEM_UNIT_DIR)
    preferred = directory / (LABEL + '.service')
    if preferred.exists():
        return preferred
    needle = str(Path(launcher))
    try:
        candidates = sorted(directory.glob('*.service'))
    except OSError:
        return None
    for path in candidates:
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith('ExecStart=') and needle in line:
                return path
    return None


def _activate(home, release_id, was_loaded, wait_seconds=60, backend='launchd'):
    """把服务切到刚装好的 release —— 安装的最后一件事。

    为什么安装器必须自己激活：Windows 的安装器 A–F 阶段会自己 enable/start
    （dist/wsl2/README.md §3.3），而 macOS 以前只写 plist、把"起来"留给操作者。
    2026-11-04 11:50 的升级就是这样静悄悄停掉的：旧 label 的 LaunchAgent 被当成遗留
    单元 bootout 掉，新 plist 写了却没人 bootstrap，`status` 里 `launchd.loaded:false`。
    安装 = 构建 + 写入 + 激活；激活失败要让这次安装以非 0 退出码结束（不静默降级）。

    三类后端各走各的路，不能互相代替：
      - `launchd` / `systemd` 用户级：CLI 的 restart（bootout+bootstrap / daemon-reload+restart）；
      - `systemd-system`（WSL2）：**不碰** —— 系统级 unit 由 `dist/wsl2/wsl/finalize-systemd.sh`
        （Windows 安装器 D 阶段）统一 enable/restart，install.py 在那边只负责切 current。
        这里明确回报"未激活"，别让调用方误以为已经在跑新 release。
    """
    if backend == 'systemd-system':
        state = host.service_state(LABEL)
        return {'attempted': False, 'ok': True, 'backend': backend, 'release_id': release_id,
                'pid': state.get('pid'), 'was_loaded': bool(was_loaded),
                'hint': ('WSL2 的系统级 unit 由 dist/wsl2/wsl/finalize-systemd.sh（安装器 D 阶段）'
                         'enable+restart 才生效；单独跑 install.py 后必须 `feishu-bridge restart`，'
                         '否则进程还跑旧 release')}
    from bridge import cli as bridge_cli
    error = None
    argv = sys.argv
    sys.argv = ['feishu-bridge', '--home', str(home), 'restart']
    try:
        bridge_cli.main(str(home))
    except SystemExit as exit_error:
        if exit_error.code not in (None, 0):
            error = 'restart 退出码 %s' % exit_error.code
    except Exception as exception:  # noqa: BLE001 - 安装器要把任何异常变成可读的失败
        error = '%s: %s' % (type(exception).__name__, exception)
    finally:
        sys.argv = argv
    status = {}
    deadline = time.time() + max(0, wait_seconds)
    while True:
        status = _read_config(Path(home) / 'status.json') or {}
        heartbeat = status.get('heartbeat_at') or 0
        if status.get('running_release') == release_id and time.time() - heartbeat < 15:
            break
        if time.time() >= deadline:
            break
        time.sleep(1)
    ok = status.get('running_release') == release_id and time.time() - (status.get('heartbeat_at') or 0) < 15
    info = {'attempted': True, 'ok': ok, 'backend': backend, 'release_id': release_id,
            'was_loaded': bool(was_loaded), 'release': status.get('running_release'),
            'pid': status.get('pid'), 'restart_error': error,
            'heartbeat_age_seconds': int(time.time() - (status.get('heartbeat_at') or 0))}
    if not ok:
        print('error: 安装已提交，但服务没有切到 %s（running_release=%s，心跳 %ss 前，restart %s）。\n'
              '       先看 %s 的尾部输出定位原因；修好后跑 feishu-bridge restart，再 feishu-bridge doctor --assert。'
              % (release_id, status.get('running_release'), info['heartbeat_age_seconds'],
                 error or '无报错但心跳没刷新', Path(home) / 'launchd.stderr.log'), file=sys.stderr)
    return info


def _reload_dsh_consumer(home, plist, consumer, kind, workspace, dsh_binary, dsh_version):
    """重载消费者单元并等它真的领过活，返回回执（供 receipt 与失败退出码用）。

    闸门与干活端同一条：`launchctl` 的 label 是全局的，临时目录里的单元一旦 reload 就会
    顶掉真机上同名 job（2026-11-19 两次 EX_CONFIG 78 都是这么来的）。
    """
    if not _service_dir_is_real(Path(plist).parent):
        return {'label': DSH_CONSUMER_LABEL, 'plist': str(plist), 'consumer': consumer,
                'kind': kind, 'workspace': str(workspace), 'dsh_binary': str(dsh_binary),
                'dsh_version': dsh_version, 'reloaded': False,
                'skipped': '临时单元目录：只写文件、不 reload'}
    was_loaded = bool(host.service_state(DSH_CONSUMER_LABEL).get('loaded'))
    reloaded = host.reload_service(DSH_CONSUMER_LABEL, plist)
    live = _wait_consumer_live(home, consumer) if reloaded.get('ok') else False
    info = {'label': DSH_CONSUMER_LABEL, 'plist': str(plist), 'consumer': consumer, 'kind': kind,
            'workspace': str(workspace), 'dsh_binary': str(dsh_binary), 'dsh_version': dsh_version,
            'was_loaded': was_loaded, 'reloaded': bool(reloaded.get('ok')),
            'pid': reloaded.get('pid'), 'live': live, 'error': reloaded.get('error')}
    if not live:
        print('error: 消费者单元已写入 %s，但 %s 秒内没看到它领取事件（reload=%s，pid=%s，%s）。\n'
              '       多半是 DSH 没登录或没有 API key：本机登录一次 dsh，或给它 DEEPSEEK_API_KEY；'
              '然后跑 feishu-bridge consumer status 看 last_seen。单元会按 15s 节流重试，'
              '不修好就一直没人答题。'
              % (plist, 45, info['reloaded'], info['pid'], info['error'] or '无报错'), file=sys.stderr)
    return info


def _agentkit_service(home, release, agent, consumer, workspace):
    """agentkit consumer 的服务定义：复用 bridge 的 plist/systemd 模板，只换 ProgramArguments。"""
    argv = [str(Path(release) / 'agentkit-consumer.py'), '--agent', agent,
            '--consumer', consumer, '--kind', AGENTKIT_KINDS.get(agent, 'other'),
            '--home', str(home)]
    if workspace:
        argv += ['--workspace', str(workspace)]
    return host.service_definition(home, sys.executable, label=AGENTKIT_LABEL,
                                   args=tuple(argv), log_prefix='agentkit-consumer')


def _prompt_signature(existing=None):
    """TTY 下当场问署名（与 Windows 安装器 -Signature 的交互一致）；回车沿用既有/内置默认。"""
    from bridge.reply import SIGNATURE as DEFAULT_SIGNATURE
    default = existing or DEFAULT_SIGNATURE
    try:
        answer = input('回复署名 [%s]: ' % default)
    except (EOFError, KeyboardInterrupt):
        return existing
    text = ' '.join(str(answer).split())
    return text or default


def _resolve_install_signature(config_path, signature=None):
    """署名由安装者决定：命令行 > 环境变量 > 既有配置 > 内置默认（TTY 下先问）。

    关键：所有可能抛错的事情（超长、非法字符）都在切换 current 之前做完。
    """
    config = _read_config(config_path)
    if signature is not None:
        return resolve_signature(config, {'SIGNATURE': signature})
    if os.environ.get('SIGNATURE') is not None:
        return resolve_signature(config, {'SIGNATURE': os.environ['SIGNATURE']})
    existing = resolve_signature(config, {})
    interactive = getattr(sys.stdin, 'isatty', lambda: False)()
    if interactive and not os.environ.get('FEISHU_INSTALL_NONINTERACTIVE'):
        return resolve_signature(config, {'SIGNATURE': _prompt_signature(existing)})
    return existing


def _systemctl(args):
    """Best-effort systemctl --user call; returns None when systemd is unavailable."""
    try:
        return subprocess.run(['systemctl', '--user'] + list(args), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def _preflight(source, home, service, link, lark_binary, config_path,
               agentkit=None, dsh=None):
    """所有会失败的前置检查集中在任何写操作之前（P2-21/D05 fail-after-mutation）。"""
    source, home, service, link = Path(source), Path(home), Path(service), Path(link)
    if not _bridge_files(source):
        raise RuntimeError('Source bridge directory has no Python files: %s' % (source / 'bridge'))
    if not Path(lark_binary).is_file():
        raise RuntimeError('Required lark-cli missing: %s' % lark_binary)
    if host.is_macos() and not (Path(sys.executable).is_file() and os.access(sys.executable, os.X_OK)):
        raise RuntimeError('安装用的 Python 解释器不可执行：%s（launcher 的 shebang 会指向它）'
                           % sys.executable)
    if host.is_linux() and any(character.isspace() for character in str(home)):
        raise RuntimeError('Linux/WSL2 安装目录不能包含空白字符：%s' % home)
    expected_launcher = (home / 'bin' / 'feishu-bridge').resolve()
    if host.is_macos():
        if service.exists():
            try:
                old = plistlib.loads(service.read_bytes())
            except (OSError, ValueError) as error:
                raise RuntimeError('Existing LaunchAgent plist is unreadable: %s' % error)
            arguments = old.get('ProgramArguments', []) if isinstance(old, dict) else []
            # 精确比较启动器路径，而不是 ``str(home) in arg``（/tmp/home 会误匹配 /tmp/home-evil）。
            if not arguments or Path(str(arguments[0])).expanduser().resolve() != expected_launcher:
                raise RuntimeError('Existing LaunchAgent is not owned by this bridge')
    elif service.exists():
        text = service.read_text(errors='replace')
        expected = ('ExecStart=%s foreground' % (home / 'bin' / 'feishu-bridge'),
                    'WorkingDirectory=%s' % home)
        if not all(line in text for line in expected):
            raise RuntimeError('Existing systemd unit is not owned by this bridge')
    launcher = home / 'bin' / 'feishu-bridge'
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != launcher.resolve():
            raise RuntimeError('Existing feishu-bridge command is not owned by this install')
    config = _read_config(config_path)
    resolve_target_open_id(config)
    if agentkit is not None:
        missing = agentkit_sources(source)
        if missing:
            raise RuntimeError('agentkit 打包缺少文件，拒绝安装：%s' % ', '.join(missing))
        if agentkit['agent'] not in AGENTKIT_KINDS:
            raise RuntimeError('未知 agentkit agent：%s（可选：%s）'
                               % (agentkit['agent'], ', '.join(sorted(AGENTKIT_KINDS))))
        if not agentkit['consumer'].strip():
            raise RuntimeError('agentkit consumer 名字不能为空（它是事件租约的持有者身份）')
        workspace = agentkit.get('workspace')
        if workspace:
            path = Path(workspace).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise RuntimeError('agentkit workspace 必须是已存在的绝对路径：%s' % workspace)
    if dsh is not None:
        if not host.is_macos():
            raise RuntimeError('--with-dsh-consumer 只在 macOS 上由 install.py 管理；'
                               'WSL2/Linux 用 dist/wsl2/wsl/install-dsh-consumer.sh（系统级 unit，需要 sudo）')
        if dsh['kind'] not in DSH_CONSUMER_KINDS:
            raise RuntimeError('未知消费者 kind：%s（可选：%s）'
                               % (dsh['kind'], ', '.join(DSH_CONSUMER_KINDS)))
        if not dsh['consumer'].strip():
            raise RuntimeError('消费者名字不能为空（它是事件租约的持有者身份）')
        workspace = Path(dsh['workspace']).expanduser()
        if not workspace.is_absolute():
            raise RuntimeError('消费者 workspace 必须是绝对路径：%s' % dsh['workspace'])
        if not (Path(source) / 'dsh-consumer.py').is_file():
            raise RuntimeError('源码里缺 dsh-consumer.py，无法装常驻消费者：%s' % source)
        # 引擎缺失必须在写任何东西之前失败（消费者的 PATH 由这里决定）。
        dsh['binary'] = str(_dsh_binary())
        dsh['version'] = _dsh_version(dsh['binary'])
        if not (Path(source) / 'dsh-worker.py').is_file():
            raise RuntimeError('源码里缺 dsh-worker.py，无法装干活端：%s' % source)
        dsh.setdefault('worker', DSH_WORKER_DEFAULT)


def install(source=None, home=None, plist=None, service=None, link=None, lark_binary=None,
            with_agentkit=False, agentkit_agent='cursor',
            agentkit_consumer='agentkit-cursor', agentkit_workspace=None, signature=None,
            with_dsh_consumer=False, dsh_consumer=DSH_CONSUMER_DEFAULT, dsh_kind='dsh',
            dsh_workspace=None, dsh_worker=DSH_WORKER_DEFAULT, activate=False,
            identity_mode=None, owner_open_ids=None, bot_app_id=None, bot_open_id=None,
            bot_app_secret_file=None):
    """构建并提交一次安装；返回 installation.json receipt。

    顺序（P2-21）：前置检查 → 构建+校验 release → 原子写 launcher → 原子切换 current（提交点）
    → 原子写 config.json / 服务定义 / launcher 链接 → 原子写 installation.json
    → **激活**（bridge restart；可选 dsh consumer 重载）→ 回写带激活结果的 installation.json。
    平台差异只在服务定义：macOS 写 LaunchAgent plist，Linux/WSL2 写 systemd user unit。
    激活是安装的一部分：两端都必须"装完即在跑新 release"（CONTRACTS.md「安装」一节）。
    """
    host.require_supported()
    source = Path(source).resolve() if source else Path(__file__).resolve().parent
    home = Path(home) if home else Path(os.environ.get('FEISHU_BRIDGE_HOME') or host.default_home())
    link = Path(link) if link else host.launcher_link()
    lark_binary = Path(lark_binary) if lark_binary else Path(
        os.environ.get('LARK_BINARY') or host.default_lark_binary())
    if host.is_macos():
        service = Path(plist) if plist else host.service_path(LABEL)
    else:
        service = Path(service) if service else host.service_path(LABEL)
    _guard_source(source)
    config_path = home / 'config.json'
    agentkit = None
    if with_agentkit:
        agentkit = {'agent': agentkit_agent, 'consumer': agentkit_consumer,
                    'workspace': agentkit_workspace or None}
    dsh = None
    if with_dsh_consumer:
        dsh = {'consumer': dsh_consumer, 'kind': dsh_kind,
               'worker': dsh_worker,
               'workspace': str(Path(dsh_workspace).expanduser()) if dsh_workspace
               else str(Path.home() / 'dsh-workspace')}
    # 先做完全部会失败的检查，再创建 home/锁文件：契约要求 preflight 在任何写操作之前。
    _preflight(source, home, service, link, lark_binary, config_path,
               agentkit=agentkit, dsh=dsh)
    # 激活前的托管状态也要在动任何东西之前记下来（回执里要能回答"升级前它是否在跑"）。
    was_loaded = bool(host.service_state(LABEL).get('loaded')) if activate else None
    # 署名必须在切 current 之前解析/校验：否则长度非法会在「代码已提交」之后才抛错。
    chosen_signature = _resolve_install_signature(config_path, signature)
    home.mkdir(parents=True, exist_ok=True)
    # install.lock：并发安装互斥；cli.lock：与 CLI 的 restart/pause 串行，避免 config 读改写被回退。
    with _flock(home / 'install.lock', '另一个 install.py 进程正在安装；请等待其结束后重试'), \
            _flock(home / 'cli.lock', '另一个 feishu-bridge CLI 命令正在执行；请稍后重试 install.py'):
        release_id = _release_id(source)
        release = home / 'releases' / release_id
        # 配置内容同样先算好（纯计算，不碰 current）：切完 current 之后只剩原子写。
        pending_config = _merge_config_defaults(_read_config(config_path))
        pending_config['release_id'] = release_id
        pending_config['target_open_id'] = resolve_target_open_id(pending_config)
        # 身份块：**一台机器一种身份**。形状与合规性都在写任何文件之前定下来（契约：preflight 先于写盘）。
        identity_block = identity_config(
            pending_config, mode=identity_mode, owner_open_ids=owner_open_ids,
            bot_app_id=bot_app_id, bot_open_id=bot_open_id,
            bot_app_secret_file=bot_app_secret_file)
        identity_mode_value, identity_notes = identity_validate(
            identity_block, target_open_id=pending_config['target_open_id'])
        for note in identity_notes:
            print('warning: %s' % note, file=sys.stderr)
        if identity_mode_value == 'bot':
            # as_bot 的长连接由安装器装的独立单元托管；桥自己再拉一个会重复投递事件。
            identity_block.setdefault('bot', {})['sidecar'] = 'service'
            # 端口：sidecar 与网关必须是同一个数（bot.py 的默认值也是 8787）；已有值不覆盖。
            identity_block['bot'].setdefault('gateway_port', INGEST_GATEWAY_PORT)
        pending_config['identity'] = identity_block
        pending_config.setdefault('sending_enabled', False)
        pending_config.setdefault('lark_binary', str(lark_binary))
        # 内置 analyst 已删除：agent_timeout / cursor_binary 这两个键没有读者了，升级时一并抹掉。
        for stale in ('agent_timeout', 'cursor_binary'):
            pending_config.pop(stale, None)
        if chosen_signature:
            pending_config['signature'] = chosen_signature
        elif not pending_config.get('signature'):
            # 没显式配署名不是错：内置模板里的 {owner} 会渲染成这台机器登录的飞书账号姓名。
            # 仍然要打一条 warning，因为"署名带谁的名字"是本人会看见的东西，不该靠猜。
            from bridge.reply import SIGNATURE as DEFAULT_SIGNATURE
            print('warning: 未指定回复署名，将使用内置模板「%s」——模板里的账号名占位符会渲染成'
                  '这台机器当前登录的飞书账号姓名；想换成固定文案用 --signature 或环境变量 '
                  'SIGNATURE 指定' % DEFAULT_SIGNATURE, file=sys.stderr)
        files = _verify_release(source, release)
        consumer_files = _copy_consumer(source, release)
        identity_sidecar_files = _copy_identity_sidecar(source, release)
        files = dict(files, **consumer_files)
        if agentkit is not None:
            agentkit['release'] = str(release)
            agentkit['release_files'] = _copy_agentkit(source, release)
        launcher = home / 'bin' / 'feishu-bridge'
        _atomic_write(launcher, _launcher_script(home).encode('utf-8'), 0o700)
        current = home / 'current'
        temp = home / 'current.new'
        if temp.is_symlink() or temp.exists():
            temp.unlink()
        temp.symlink_to(release, target_is_directory=True)
        temp.replace(current)
        config = pending_config
        _atomic_json(config_path, config)
        if host.is_macos():
            _atomic_write(service, plistlib.dumps(host.service_definition(home, launcher, label=LABEL,
                                                                      args=('foreground',))), 0o600)
            service_info = {'plist': str(service), 'service_backend': 'launchd',
                            'retired_consumer_removed': _remove_retired_consumer(home, service),
                            'legacy_services_removed': _remove_legacy_services(home, Path(service).parent)}
            if agentkit is not None:
                agentkit_path = _service_path_keeping_parked(
                    service.with_name(AGENTKIT_LABEL + '.plist'), home)
                definition = _agentkit_service(home, release, agentkit['agent'],
                                               agentkit['consumer'], agentkit['workspace'])
                _atomic_write(agentkit_path, plistlib.dumps(definition), 0o600)
                agentkit['plist'] = str(agentkit_path)
                agentkit['service_backend'] = 'launchd'
        else:
            system_unit = _system_unit_for(home, launcher)
            unit = host.service_definition(home, launcher, label=LABEL, args=('foreground',))
            if system_unit is not None:
                # 系统级 unit 由 dist/wsl2/wsl/finalize-systemd.sh 托管：这里再写一份用户级 unit
                # 会出现两个 enabled 单元抢同一 home（谁先起谁赢），升级时更难排查。
                service_info = {'service_unit': str(system_unit),
                                'service_backend': 'systemd', 'service_scope': 'system',
                                'service_enabled': True,
                                'service_note': ('检测到系统级 unit %s：跳过用户级 unit 的写入与 enable，'
                                                 '托管状态以 finalize-systemd.sh / verify.sh 为准'
                                                 % system_unit)}
                if system_unit.name != LABEL + '.service':
                    service_info['service_name_warning'] = (
                        '系统级 unit 仍用旧名 %s：本次安装没有按新名重建它（升级的 D 阶段会迁移）'
                        % system_unit.name)
                reload = None
            else:
                _atomic_write(service, unit.encode('utf-8'), 0o600)
                reload = _systemctl(['daemon-reload'])
                enable = _systemctl(['enable', service.name])
                service_info = {'service_unit': str(service),
                                'service_backend': 'systemd', 'service_scope': 'user',
                                'service_enabled': bool(enable and enable.returncode == 0)}
            if agentkit is not None:
                unit_path = service.with_name(AGENTKIT_LABEL + '.service')
                _atomic_write(unit_path,
                              _agentkit_service(home, release, agentkit['agent'],
                                                agentkit['consumer'],
                                                agentkit['workspace']).encode('utf-8'), 0o600)
                agentkit['service_unit'] = str(unit_path)
                agentkit['service_backend'] = 'systemd'
            if reload is None or reload.returncode:
                service_info['service_warning'] = (
                    'systemd --user 不可用或 daemon-reload 失败；启用 systemd 后运行 feishu-bridge start')
        if not (link.is_symlink() and link.resolve() == launcher.resolve()):
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(launcher)
        dsh_plist = None
        worker_plist = None
        if dsh is not None:
            workspace = Path(dsh['workspace'])
            workspace.mkdir(parents=True, exist_ok=True)
            # 与 bridge 单元同一个目录：自定义 --plist 的安装（含测试）不会写到真机的 LaunchAgents。
            dsh_plist = _write_dsh_consumer(home, Path(service).parent, dsh['consumer'],
                                            dsh['kind'], workspace, dsh['binary'])
            service_info['dsh_consumer_plist'] = str(dsh_plist)
            # 干活端与回话端一起装：批准之后总得有人干（PR-3 起；S3a 之前它只是随发布躺着）。
            worker_plist = _write_dsh_worker(home, Path(service).parent, dsh['worker'],
                                             dsh['binary'])
            service_info['dsh_worker_plist'] = str(worker_plist)
        if agentkit is None:
            _warn_stale_agentkit(service, home, release_id)
        _rewrite_external_config(home)
    # 保留策略：current 指向新 release 之后，才清理旧构建产物（FEISHU_RELEASE_KEEP=0 关闭）。
        pruned = _prune_releases(home, release_id)
        receipt = {'home': str(home), 'release_id': release_id, 'label': LABEL, **service_info,
                   'command': str(link), 'sending_enabled': bool(config.get('sending_enabled')),
                   'source_sha256': _release_digest(source), 'installed_at': int(time.time()), 'files': files,
                   'consumer_files': consumer_files,
                   'pruned_releases': pruned,
                   'note': ('installed and activated: 写盘后已执行 bridge restart 并等心跳刷新，'
                            '结果在 receipt.activation；CLI 在激活失败时以退出码 3 结束')}
        if agentkit is not None:
            receipt['agentkit'] = agentkit
            receipt['agentkit']['note'] = ('LaunchAgent 只写不加载：先 agentkit-consumerctl.py register '
                                           '注册事件消费者，再 start 生效；换 release 后必须 restart'
                                           '（bootout+bootstrap，只 kickstart 会跑旧 release）')
        _atomic_json(home / 'installation.json', receipt)
    # 锁已释放：激活要走 CLI 的 restart（它自己拿 cli.lock，且在锁内调用会死锁）。
    activation = None
    if activate:
        backend = ('systemd-system' if service_info.get('service_scope') == 'system'
                   else service_info.get('service_backend') or ('launchd' if host.is_macos() else 'systemd'))
        activation = _activate(home, release_id, was_loaded, backend=backend)
    dsh_result = None
    worker_result = None
    if dsh is not None:
        dsh_result = _reload_dsh_consumer(home, dsh_plist, dsh['consumer'], dsh['kind'],
                                         Path(dsh['workspace']), dsh['binary'], dsh['version'])
        worker_result = _reload_dsh_worker(home, worker_plist, dsh['worker'],
                                           dsh['binary'])
    elif activate and host.is_macos():
        # 升级不带 --with-dsh-consumer 时也要让它吃到新 release（否则回复链路跑旧代码）。
        dsh_result = _reload_existing_dsh_consumer(home, Path(service).parent)
        worker_result = _ensure_dsh_worker(home, Path(service).parent, source)
    # 身份侧车：bot 模式装一套；user 模式**清掉**（一台机器一种身份，不许残留）。
    identity_result = None
    ingest_gateway_result = None
    if activate:
        if identity_mode_value == 'bot':
            # 先有 token 与回环网关，再起 sidecar：sidecar 启动第一件事就是读 token。
            ingest_gateway_result = install_ingest_gateway(home, identity_block)
            identity_result = install_identity_sidecar(home, identity_block)
        else:
            ingest_gateway_result = retire_ingest_gateway(home)
            identity_result = retire_identity_sidecar(home)
    receipt['identity'] = {'mode': identity_mode_value,
                           'sidecar': identity_result,
                           'ingest_gateway': ingest_gateway_result,
                           'owner_open_ids': list(identity_block.get('owner_open_ids') or [])}
    receipt['activation'] = {'bridge': activation, 'dsh_consumer': dsh_result,
                             'dsh_worker': worker_result}
    _atomic_json(home / 'installation.json', receipt)
    return receipt


def _rewrite_external_config(home):
    """external.json 只剩一个事实（reply_timeout_seconds）：顺手抹掉升级前遗留的 enabled。

    老版本用它当"外部消费者开关"；开关删除后这个键没有读者，留着会让读文件的人以为还能关。
    """
    path = Path(home) / 'external.json'
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        print('warning: %s 无法解析，保持原样；`feishu-bridge events timeout <秒>` 可重写它' % path,
              file=sys.stderr)
        return
    if not isinstance(data, dict) or set(data) <= {'reply_timeout_seconds'}:
        return
    timeout = data.get('reply_timeout_seconds')
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        timeout = 600
    _atomic_json(path, {'reply_timeout_seconds': timeout})
    print('note: external.json 已收敛为 {"reply_timeout_seconds": %d}（enabled 开关已删除，不再读）'
          % timeout, file=sys.stderr)


def _warn_stale_agentkit(service, home, release_id):
    """agentkit consumer 的 plist 指向绝对 release 路径（故意不跟随 current）。

    所以只升级 bridge（不带 --with-agentkit）时，consumer 仍在旧代码上跑 —— 这条流水线最怕
    「bridge 新、consumer 旧」的隐性不兼容，必须在安装输出里说出来。
    """
    path = Path(service).with_name(AGENTKIT_LABEL + ('.plist' if host.is_macos() else '.service'))
    if not path.exists():
        return
    try:
        text = path.read_text(errors='replace')
    except OSError:
        return
    match = re.search(r'releases/([0-9a-f]{16})', text)
    if match and match.group(1) != release_id:
        print('warning: agentkit consumer 仍指向旧 release %s（本次装的是 %s）。\n'
              '         要一起升级：python3 install.py --with-agentkit …（沿用原 consumer 名），'
              '再 python3 agentkit-consumerctl.py restart'
              % (match.group(1), release_id), file=sys.stderr)


def verify_installation(home=None, wait_seconds=60, restart=True):
    """装完就断言（macOS / Linux 同一套退出码语义，对应 Windows 安装器的 F 阶段）。

    做三件事：把服务切到当前 release（`restart` 为真时；install() 已经激活过就传 False，
    避免同一次安装重启两遍）→ 等心跳刷新到新 release → 跑 `doctor --assert`。
    返回 doctor 的退出码：0 通过，1 有 fail 级检查项。
    """
    from bridge import cli as bridge_cli
    home = Path(home) if home else Path(os.environ.get('FEISHU_BRIDGE_HOME') or host.default_home())
    if restart:
        argv = sys.argv
        sys.argv = ['feishu-bridge', '--home', str(home), 'restart']
        try:
            bridge_cli.main(str(home))
        except SystemExit as error:
            if error.code not in (None, 0):
                print('restart 失败（%s）：服务没切到新 release，下面的断言会如实反映' % error.code,
                      file=sys.stderr)
        finally:
            sys.argv = argv
    installed = _read_config(home / 'installation.json').get('release_id')
    status_path = home / 'status.json'
    deadline = time.time() + max(0, wait_seconds)
    while time.time() < deadline:
        status = _read_config(status_path)
        heartbeat = status.get('heartbeat_at') or 0
        if (time.time() - heartbeat < 15) and status.get('running_release') == installed:
            break
        time.sleep(1)
    else:
        print('warning: %d 秒内没等到新 release %s 的心跳（继续断言当前状态）' % (wait_seconds, installed),
              file=sys.stderr)
    code = bridge_cli._doctor(home, host.service_path(LABEL), ['--assert'])
    if code != 0:
        # 刚重启的几秒里 launchd/systemd 的 pid 与 status.json 里的 pid 可能还差一拍
        # （doctor 的 service_running 会判 fail），5 秒后重跑一次再定论 —— 与
        # tools/bridge-remote.sh check 同一条重试策略。真问题重试仍然是 fail。
        print('doctor 第一次断言未通过，5 秒后重试一次（刚重启时 pid/心跳可能还没对齐）', file=sys.stderr)
        time.sleep(5)
        code = bridge_cli._doctor(home, host.service_path(LABEL), ['--assert'])
    return code


def main():
    host.require_supported()
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='安装/升级飞书桥接服务（可选带上 agentkit 并行入口）')
    parser.add_argument('--with-agentkit', action='store_true',
                        help='把 agentkit 运行时打包进 release，并写一个**不加载**的 consumer LaunchAgent')
    parser.add_argument('--agentkit-agent', default='cursor', choices=sorted(AGENTKIT_KINDS),
                        help='agentkit 使用哪个 AGENT 适配器（默认 cursor）')
    parser.add_argument('--agentkit-consumer', default='agentkit-cursor',
                        help='事件消费者名字（默认 agentkit-cursor；不能与既有 consumer 同名）')
    parser.add_argument('--agentkit-workspace', default=None,
                        help='AGENT 的工作空间（绝对路径，默认不传 = 私有临时目录）')
    parser.add_argument('--with-dsh-consumer', action='store_true',
                        help='把 current/dsh-consumer.py 装成常驻消费者 LaunchAgent（macOS；'
                             'WSL2/Linux 用 dist/wsl2/wsl/install-dsh-consumer.sh）。'
                             '桥自己不再回答任何消息：没有消费者 = 收得到、没人回')
    parser.add_argument('--dsh-consumer', default=DSH_CONSUMER_DEFAULT,
                        help='消费者身份名（默认 %s，与 install-dsh-consumer.sh 一致）' % DSH_CONSUMER_DEFAULT)
    parser.add_argument('--dsh-kind', default='dsh', choices=DSH_CONSUMER_KINDS,
                        help='消费者领取的事件 kind（默认 dsh）')
    parser.add_argument('--dsh-workspace', default=None,
                        help='Agent 工作区（默认 ~/dsh-workspace，Windows 脚本同一默认值）')
    parser.add_argument('--signature', default=None, help=SIGNATURE_HELP)
    parser.add_argument('--identity-mode', default=None, choices=('user', 'bot'),
                        help='身份模式：user=个人账号（默认）；bot=机器人应用。'
                             '**一台机器只能一种**，切换即等同换一套收发通道')
    parser.add_argument('--owner-open-id', action='append', default=None,
                        metavar='ou_...', help='bot 模式下谁能批准/谁能发命令（可重复）')
    parser.add_argument('--bot-app-id', default=None, metavar='cli_...',
                        help='bot 模式复用的飞书应用 app_id（不新建应用）')
    parser.add_argument('--bot-open-id', default=None, metavar='ou_...',
                        help='bot 模式下机器人自己的 open_id（强烈建议；缺省时 @ 判定放宽）')
    parser.add_argument('--bot-app-secret-file', default=None, metavar='FILE',
                        help='bot 模式的应用密钥文件（0600）；或先 lark-cli config keychain-downgrade')
    parser.add_argument('--verify', action='store_true',
                        help='装完再跑一次 doctor --assert（激活已由安装器完成；有 fail 时退出码 1）')
    args = parser.parse_args()
    receipt = install(with_agentkit=args.with_agentkit, agentkit_agent=args.agentkit_agent,
                      agentkit_consumer=args.agentkit_consumer,
                      agentkit_workspace=args.agentkit_workspace,
                      with_dsh_consumer=args.with_dsh_consumer,
                      dsh_consumer=args.dsh_consumer, dsh_kind=args.dsh_kind,
                      dsh_workspace=args.dsh_workspace,
                      signature=args.signature, activate=True,
                      identity_mode=args.identity_mode,
                      owner_open_ids=args.owner_open_id,
                      bot_app_id=args.bot_app_id,
                      bot_open_id=args.bot_open_id,
                      bot_app_secret_file=args.bot_app_secret_file)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    activation = receipt.get('activation') or {}
    bridge_ok = bool((activation.get('bridge') or {}).get('ok'))
    consumer = activation.get('dsh_consumer')
    consumer_ok = consumer is None or bool(consumer.get('live'))
    if consumer is None:
        # 桥只负责收发：没有消费者就没人回答。不能装作装完了。
        print('warning: 本次安装没有装/重载任何事件消费者。桥会继续收消息并写事件，'
              '但没有大脑回答——下一步：python3 install.py --with-dsh-consumer'
              '（或按 CONSUMER-TEMPLATE.md 接一个外部消费者），然后 feishu-bridge doctor --assert 确认 '
              'consumer_offline 不再是 fail', file=sys.stderr)
    if not (bridge_ok and consumer_ok):
        # 3 = 代码已提交但没跑起来（与 Windows 安装器「3 systemd 未就绪」同一个含义）。
        raise SystemExit(3)
    if args.verify:
        raise SystemExit(verify_installation(receipt['home'], restart=not bridge_ok))


if __name__ == '__main__':
    main()
