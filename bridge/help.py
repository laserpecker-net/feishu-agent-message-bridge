"""使用说明书：正文、切分——**唯一一处**。

正文里的命令表**从 :mod:`bridge.commands` 生成**（:func:`bridge.commands.table_lines`），
所以「说明书写了 A、代码只认 B」这种事在结构上不可能发生；
``tests/test_commands.py`` 里还有一条一致性用例兜底。

另外两件事也在这里：

* 切分（:func:`chunks`）：按段切，单条不超过 ``MAX_CHARS``，多条带「（第 i/n 条）」，
  最后一条带「（完）」，避免几条消息在飞书里看串；
* 命令判定**不在这里**：那是 :func:`bridge.commands.match` 的事（确定性字符串匹配）。

``help`` 是一条**命令**，不进教材、不建任务、不产生待答问题：服务端处理完就把这条消息
落成 ``ignored``，不交给事件管道。
"""

import re

from . import commands
from .transport import mentioned, message_text  # noqa: F401  （wants 兼容旧调用点用）

#: 单条消息的字数上限（飞书单条能长很多，这里按"一屏能读完"来定）。
MAX_CHARS = 1200

#: 说明书是给人看的：这些词一律不许出现（``tests/test_help_manual.py`` 会逐条扫）。
BANNED = ('账本', '审计', '链路', 'running', 'hints', 'outbox', 'approval',
          'state=', 'schema', '迁移', '幂等', 'release', 'sqlite')

_TEMPLATE = """我是你在飞书里的助手：替你在群里回话，也替你把要动手的事写成一张卡，等你点头再干。

【命令（要一个字不差地发）】
{commands}

命令里只有「执行」可以带编号：`状态` 会把等你批的卡按发出先后编成 1、2、3，
`执行 2` 就是批第 2 张；编号以 `状态` 最近一次列出来的为准。

【我平时怎么工作】
群里有事找我：在群里 @ 我，把要求说清楚，我回话；需要动手的事，我会私聊发你一张卡。
我不会自己定时找你。我发消息只有两种时候：群里有事，或者我起草了一张卡等你点头。

【批卡时回的三句话（这三句不是命令，是对那张卡的回答）】
- 做：我开始干。
- 不做：不干，这件事作罢。
- 做，但只做①：只做第一步，其余不动。
有时候我会请你「引用那张卡再回」：同时等你的卡不止一张，只有引用才分得清你要批哪一张。

【教我记得】
发「记住：汇报先给结论」，我记一条，并回执告诉你记在哪、记了什么。
发「撤回」，我把最近记的一条撤掉，历史留着不删；发「清单」，我列出现在生效的条目。
每条记忆都有一句回执。如果你觉得记得不对，随时让我撤。

【上下班】
发「下班」：我不再收消息、不再回话；这段时间群里的事我不补，等你上班再说，免得一次涌一堆。
发「上班」：我接着干。
有一点要记得：我发给你的卡，三十分钟没人批就作废，所以下班前别把卡批到一半。

【我不做什么】
- 别人让我做的事，我只起草，绝不替你点头。
- 不撤回不是你发的消息，也不在群里乱发。
- 拿不准的事，我宁可先问你一句，不自己猜着办。
- 群里的命令一律不执行：命令只在你私聊我时才算数。

【出问题怎么办】
我没回话：先在群里看一眼我最近有没有回执；一句都没有，多半是我没在跑，等两分钟再看。
还想不通就发「状态」，我会自己报；实在不行再找人看。

随时发 help，我就再发一遍这份说明。"""

#: 说明书全文（终端 ``feishu-bridge help`` 与飞书里发 ``help`` 是同一份）。
MANUAL = _TEMPLATE.format(commands='\n'.join(commands.table_lines()))


def text():
    """说明书全文。"""
    return MANUAL


def wants(message, *, owner_id, self_chat_ids, allow_any_private=False):
    """兼容旧调用点：这条消息是不是 ``help`` 命令。

    判定已收归 :func:`bridge.commands.match`；这里只补"谁能发、在哪发"的边界：
    本人发的、私聊（白名单会话或任意一对一私聊）。
    """
    if not owner_id:
        return False
    chat_id = message.get('chat_id')
    if not isinstance(chat_id, str):
        return False
    if chat_id not in (self_chat_ids or ()) and not allow_any_private:
        return False
    sender = message.get('sender') or {}
    if sender.get('id') != owner_id or sender.get('sender_type') == 'app':
        return False
    if not allow_any_private and not mentioned(message, owner_id):
        return False
    found = commands.match(message_text(message))
    return bool(found and found[0] == 'help')


def _split_long(paragraph, budget):
    """一段超过 budget 时按句号切开，尽量不让一句话被劈成两半。"""
    if len(paragraph) <= budget:
        return [paragraph]
    pieces, current = [], ''
    for sentence in re.split(r'(?<=。)', paragraph):
        if not sentence:
            continue
        if current and len(current) + len(sentence) > budget:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    # 单句本身还超长：只能硬切，别丢字。
    result = []
    for piece in pieces:
        while len(piece) > budget:
            result.append(piece[:budget])
            piece = piece[budget:]
        if piece:
            result.append(piece)
    return result or [paragraph[:budget]]


def chunk_text(text, max_chars=MAX_CHARS):
    """把一段长文字按段切成若干条，每条 ≤ max_chars，带「（第 i/n 条）」，末条带「（完）」。

    切分规则只有这一处：说明书用它，干活端的交接单也用它（``bridge/report.py``）。
    两处各写一份分条规则，早晚会出现"同一个东西在私聊里长得不一样"。
    """
    budget = max(64, int(max_chars) - 40)
    paragraphs = [item.strip() for item in str(text or '').split('\n\n') if item.strip()]
    groups, current = [], ''
    for paragraph in paragraphs:
        for piece in _split_long(paragraph, budget):
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= budget:
                current = current + '\n\n' + piece
            else:
                groups.append(current)
                current = piece
    if current:
        groups.append(current)
    total = len(groups)
    out = []
    for index, group in enumerate(groups, 1):
        head = '（第 %d/%d 条）\n' % (index, total) if total > 1 else ''
        tail = '\n（完）' if index == total else ''
        body = head + group + tail
        if len(body) > max_chars:  # 头部+尾部超了预算就再切一刀
            body = head + _split_long(group, max_chars - len(head) - len(tail))[0] + tail
        out.append(body)
    return out


def chunks(max_chars=MAX_CHARS):
    """说明书正文的分条结果（规则见 :func:`chunk_text`）。"""
    return chunk_text(MANUAL, max_chars)
