"""精确命令表：**飞书里能对桥说的话，只有这张表里列出的那几个词**。

为什么要有这张表（2026-11-17 主人拍板）：

* 说明书里的每一句都是**命令**，必须一个字不差；「停一下」「继续」这类口语说法
  **不算**——它们不是命令，是聊天。命令与聊天的边界必须是一条直线，不能由模型去猜。
* 所以判定是**确定性字符串匹配**（:func:`match`），不经过任何模型推断；
  说明书正文（:mod:`bridge.help`）也从这张表生成，杜绝「说明书写了 A、代码只认 B」。

三个入口只在这里定义一次：

1. :data:`COMMANDS` —— 命令原文与它做什么（说明书逐字引用它）；
2. :func:`match` —— 一条消息是不是命令、是哪一条；
3. :func:`looks_like_command` —— 不是精确命令、但明显在喊它（用来回一句"命令要精确"）。

``下班`` / ``上班`` 的判定复用 :mod:`bridge.hold`（挂起逻辑只有那一份实现），
这里只负责把「同一个开关的两种说法」收进同一张表。
"""

import re

from . import hold

#: 提到某人时飞书塞进正文的占位符。
_MENTION = re.compile(r'@_user_\d+')

#: 命令原文 → 一条命令的四件事。**键就是主人要逐字发的那个词**，说明书照抄它。
#:
#: ``what`` / ``when`` / ``example`` 是给说明书用的：主人要的是"什么时候用、长什么样"，
#: 不是功能复述。写这三行时一律用日常词，别把系统词塞进来（说明书有禁用词门禁）。
COMMANDS = {
    'help': {
        'verb': 'help',
        'what': '再看一遍这份说明书。',
        'when': '忘了某个命令怎么用，或者想确认我现在按什么规矩办事。',
        'example': '`help`',
    },
    '执行': {
        'verb': 'execute',
        'what': '批准那张等你点头的卡，把它推到「开始干」。',
        'when': '你收到卡片、看完觉得没问题，又不想打字回「做」时。',
        'example': '`执行`（只有一张待批）；`执行 2`（两张以上，挑第 2 张）',
    },
    '状态': {
        'verb': 'status',
        'what': '报一下我在不在跑、有没有下班、哪几张卡等你批（带编号）。',
        'when': '你不确定我是不是还活着，或者想不起来有几件事卡在你这里时。',
        'example': '`状态`',
    },
    '上班': {
        'verb': 'release',
        'what': '恢复接待：重新开始收消息、回话。',
        'when': '你下班前发过「下班」，现在回来了。',
        'example': '`上班`',
    },
    '下班': {
        'verb': 'hold',
        'what': '停止收消息、不再回话；这段时间群里的事我不补。',
        'when': '你要去开会、下班、休假，不想我在这期间插话时。',
        'example': '`下班`',
    },
    '记住：': {
        'verb': 'remember',
        'what': '记一条，冒号后面接要说的话。',
        'when': '你希望我以后都按这个来，比如汇报口径、称呼、忌讳。',
        'example': '`记住：汇报先给结论`',
    },
    '撤回': {
        'verb': 'revert',
        'what': '撤回最近记的一条（历史留着不删）。',
        'when': '你发现我记错了，或者那条规矩不再适用。',
        'example': '`撤回`',
    },
    '清单': {
        'verb': 'list',
        'what': '列出现在生效的记忆条目。',
        'when': '你想知道我到底记住了哪些规矩。',
        'example': '`清单`',
    },
}

#: 带参数的命令（后面必须跟内容，否则不算命令）。
PREFIX_COMMANDS = ('记住：', '记住:')

#: 说明书里按这个顺序列命令。
ADVERTISED = ('help', '执行', '状态', '上班', '下班', '记住：', '撤回', '清单')

#: 看得出在喊它、但不是精确命令的说法。只用来回一句引导语，**绝不执行**。
NEAR_MISS = frozenset((
    'stop', 'pause', 'resume', 'on', 'off',
    '停一下', '停下', '暂停', '停', '继续', '恢复', '取消', '停下来',
    '下班了', '上班了', '下班吧', '上班吧', '下班了吗',
    '帮助', '说明书', '怎么用', '状态呢', '状态如何', '现在什么状态',
    '清单呢', '撤回一下', '撤回刚才那条', '记一下', '记住', '记住一下',
))

#: 不是命令时的统一引导语：只报**精确**命令，不给近似说法留口子。
GUIDE = ('没听懂。命令要精确，一个字不差：\n'
         '执行 / 状态 / 上班 / 下班 / 清单 / 撤回 / help\n'
         '批第几张写「执行 2」；记东西用「记住：……」')


def _clean(text):
    """去掉 @ 占位符与首尾空白；句末只允许一个句号或感叹号。"""
    value = _MENTION.sub('', text or '').strip()
    value = value.strip()
    for punctuation in ('。', '！', '.', '!'):
        if value.endswith(punctuation):
            value = value[:-1].strip()
            break
    return value


def match(text):
    """是命令就返回 ``(动词, 参数)``，否则 ``None``。

    只认精确词：去掉 @ 占位符、首尾空白与**一个**句末标点之后，必须与表里逐字相等。
    ``记住：`` 与 ``执行`` 是唯一可能带参数的：前者冒号后必须有内容，后者后面可跟一个编号。
    """
    value = _clean(text)
    if not value:
        return None
    for word in PREFIX_COMMANDS:
        if value.startswith(word):
            payload = value[len(word):].strip()
            return ('remember', payload) if payload else None
    # 「执行」：只认"执行"或"执行 <数字>"，别的写法（执行了 / 执行一下 / 执行 2 张）不算命令。
    if value.startswith('执行'):
        rest = value[len('执行'):].strip()
        if not rest:
            return ('execute', None)
        return ('execute', rest) if rest.isdigit() else None
    lowered = value.lower()
    entry = COMMANDS.get(value) or COMMANDS.get(lowered)
    if entry is not None and entry['verb'] != 'remember':
        # 「上班」「下班」也在这张表里（verb 是 release / hold）：命令只有这两个词，
        # 不再有「挂起/恢复/on/off」这类同义词（2026-11-18 主人拍板：命令必须精确）。
        return (entry['verb'], None)
    return None


def looks_like_command(text):
    """是不是"在喊它但不精确"。只用于回引导语；返回 True 不代表要执行任何东西。"""
    value = _clean(text)
    if not value:
        return False
    lowered = value.lower()
    if lowered in NEAR_MISS or value in NEAR_MISS:
        return True
    # 命令词打头但后面跟了别的东西（如「下班了」「记住」没写内容）。
    for word in ('下班', '上班', '状态', '清单', '撤回', '记住', 'help', '执行'):
        if (value.startswith(word) or lowered.startswith(word)) and len(value) <= 24:
            return True
    return False


def guide():
    """不精确时的回答（终端与飞书同一份）。"""
    return GUIDE


def table_lines():
    """说明书里的命令表：每条命令四行，**逐字照** :data:`COMMANDS`。

    四行是主人 2026-11-18 拍板的格式：光说"做什么"不够，还要"什么时候用"和"示例"，
    否则命令表和词典没区别。命令行 ``feishu-bridge commands`` 也用这一份。
    """
    lines = []
    for word in ADVERTISED:
        entry = COMMANDS[word]
        lines.append('- ' + word)
        lines.append('  做什么：' + entry['what'])
        lines.append('  什么时候用：' + entry['when'])
        lines.append('  示例：' + entry['example'])
    return lines
