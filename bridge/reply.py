"""回复正文的拼装：署名、密钥拦截、账号自称（Python 3.9 stdlib）。没有子进程，没有 agent。

从 ``bridge/agent.py``（内置 Cursor 分析器）里搬出来：这些是"每条回复都要走"的纯函数，
和"用什么 agent 干活"无关。搬出来之后，删掉内置分析器不会连带删掉回复的署名与脱敏。
"""
import re

from agent_prompt import normalize_owner_name
from agent_prompt import owner_name_from_status

OWNER_PLACEHOLDER = '{owner}'

#: 取不到账号身份时占位符的替换词。刻意中性、不含任何具体人名；替换后的文本与升级前的
#: 兜底署名逐字相同（「此回复信息来自账号本人的 AI 助手」），所以取不到身份时行为不变。
NEUTRAL_SIGNATURE_OWNER = '账号本人'

#: 内置兜底署名模板。真正的署名仍由安装者决定（install.py --signature / 安装器交互提问），
#: 写进 config.json 的 signature，render_reply 每次读取；不配就用这个模板。模板本身不含
#: 任何具体人名，账号名在渲染时才进来——写死人名会在别人的机器上静默继承成「某某的 AI 助手」。
SIGNATURE = '此回复信息来自%s的 AI 助手' % OWNER_PLACEHOLDER
MAX_SIGNATURE_LENGTH = 60
MAX_OUTPUT = 8 * 1024 * 1024
#: 最终回复正文（含署名）的字符上限：飞书一条消息放不下更长的内容。
MAX_REPLY_CHARS = 6000

BLOCKED_REPLY = '这条回复里带有密钥或密码，已拦截，没有发出原文。'

LEAKED_SECRET = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r'|\bAKIA[0-9A-Z]{16}\b|\bASIA[0-9A-Z]{16}\b'
    r'|\bgh[pousr]_[A-Za-z0-9]{20,}\b'
    r'|\bgithub_pat_[A-Za-z0-9_]{20,}\b'
    r'|\bglpat-[A-Za-z0-9_-]{16,}\b'
    r'|\bxox[baprs]-[A-Za-z0-9-]{10,}\b'
    r'|\bsk-[A-Za-z0-9_-]{16,}\b'
    r'|\bsk_(?:live|test)_[A-Za-z0-9]{10,}\b'
    r'|\bAIza[0-9A-Za-z_-]{30,}\b'
    r'|\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'
    r'|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
    r'|[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s@/]{3,}@[^\s/]+'
    r'|(?i:(?<![A-Za-z0-9])(?:api[_-]?key|apikey|secret|password|passwd|token|access[_-]?key|client[_-]?secret|private[_-]?key)(?![A-Za-z0-9])\s*[:=]\s*[\'"]?[A-Za-z0-9+/_=-]{16,})')



def redact_secret_reply(text):
    """Drop outbound text that contains key or password material."""
    if isinstance(text, str) and LEAKED_SECRET.search(text):
        return BLOCKED_REPLY
    return text


def normalize_signature(value):
    """把安装者填的署名洗成一行短文本；空/非字符串一律回落到内置兜底值。

    这里只做"清洗"（折叠空白、限长），**不做占位符替换**：`{owner}` 要留到渲染时用账号身份
    替换，否则安装期就把一个名字固化了。
    """
    if not isinstance(value, str):
        return SIGNATURE
    text = ' '.join(value.split())
    if not text:
        return SIGNATURE
    return text[:MAX_SIGNATURE_LENGTH]


def resolve_signature(signature=None, owner=None):
    """把署名模板渲染成一行文本：`{owner}` → 本机飞书账号姓名（取不到用中性词）。

    模板来自 `config.json.signature` 或内置兜底；账号名只由调用方传入的 `owner` 决定
    （bridge 侧来自 `status.json` 的 `identity`，与正文自称同一个来源、同一份实现）。
    不含占位符的旧署名（例如 Windows 机器上显式配的「来自某某的AI助手」）原样返回，
    升级不改任何既有署名的字面值。
    """
    text = normalize_signature(signature)
    if OWNER_PLACEHOLDER in text:
        text = text.replace(OWNER_PLACEHOLDER, normalize_owner_name(owner) or NEUTRAL_SIGNATURE_OWNER)
    return text[:MAX_SIGNATURE_LENGTH]


def owner_name_at(home):
    """bridge 侧（gateway / CLI，各自独立进程）取本机账号姓名；只转发到那一份实现。"""
    return owner_name_from_status(home)


#: 飞书文本消息里的 @ 语法：`<at user_id="ou_…"></at>`（名字留空由客户端按 id 渲染——
#: 写死名字会在对方改名后对不上）。
MENTION_TAG = '<at user_id="%s"></at>'
AT_TAG = re.compile(r'<at\s+user_id="([^"]+)"\s*>.*?</at>', re.S)
#: 消息原文里的 @ 占位符（`@_user_1`）：只在收消息时出现，正文里出现说明模型把它抄了进去。
AT_PLACEHOLDER = re.compile(r'@_user_\d+')


def mention_tag(open_id):
    """@ 某个人的标签；没有 id 就不加（宁可不 @，也不要凭空造一个 id）。"""
    if not isinstance(open_id, str) or not open_id.startswith('ou_'):
        return ''
    return MENTION_TAG % open_id


def scrub_mentions(text, self_open_id=None):
    """正文里**不能有**的两类 mention：@ 本人、以及抄进正文的 `@_user_1` 占位符。

    ① 回复是用本人账号发出去的，@ 自己等于让他在群里再被提醒一次（2026-11-10 本人指出：
    「肯定不能@我吧」）；② 占位符是收消息时的协议记号，抄进正文等于把内部编码发给群里
    （2026-11-07 真机抄过一次）。两条都由这里硬保证，不靠模型自觉。
    """
    if not isinstance(text, str) or not text:
        return text
    if isinstance(self_open_id, str) and self_open_id:
        text = AT_TAG.sub(lambda m: '' if m.group(1) == self_open_id else m.group(0), text)
    if AT_PLACEHOLDER.search(text):
        # 占位符连同它两侧的空白一起拿掉；剩下的多余空格与中文标点前的空格一并收干净，
        # 正文的换行段落保持原样（不把整段压成一行）。
        text = re.sub(r'[ \t]*@_user_\d+[ \t]*', '', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'[ \t]+([，。、；：！？）])', r'\1', text)
    return text


def render_reply(answer, signature=None, owner=None, receipt=None, mention=None, self_open_id=None):
    """拼出最终回复：@提问的人 → 正文 → 署名 → 空行 → 回执。

    ``mention`` 是这条消息的发送者 open_id：群里回复统一 @ 他（「别人 @ 我，回复当然 @ 他」），
    私聊不传。``self_open_id`` 是本机账号：正文里任何指向自己的 @ 都会被删掉——
    回复本来就是用本人账号发的，再 @ 自己等于让他被提醒两次。

    ``receipt`` 是"这条消息实际做了什么"的几行（记在哪个名称下、撤了哪条、提醒设在几点）。
    它必须排在署名**后面**：署名是落款，回执是附注——落款夹在附注中间读起来像没写完
    （2026-11-06 本人指出）。传参而不是让运行时把回执拼进正文，是因为顺序要一处说了算：
    正文由模型写、回执由运行时写、落款由 bridge 写，三段各归各家。
    """
    if not isinstance(answer, str):
        raise ValueError('Answer must be text')
    signature = resolve_signature(signature, owner)
    text = redact_secret_reply(answer).strip()
    text = scrub_mentions(text, self_open_id)
    # @ 提问的那个人：由 bridge 统一加在前面，不让模型自己拼——模型拼错就是抄占位符或 @ 本人。
    tag = mention_tag(mention)
    if tag and tag not in text and (not self_open_id or mention != self_open_id):
        text = tag + ' ' + text.strip()
    while text.endswith(signature):
        text = text[:-len(signature)].rstrip()
    if not text:
        raise ValueError('Answer must not be empty')
    trailer = '\n' + signature
    if isinstance(receipt, str) and receipt.strip():
        note = redact_secret_reply(receipt).strip()
        trailer += '\n\n' + note
    # 超长时截正文、不截落款和回执：署名被截掉等于整条消息没了落款，回执被截掉等于做了不说。
    text = text[:max(MAX_REPLY_CHARS - len(trailer), 1)].rstrip()
    return text + trailer

