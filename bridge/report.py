"""把"活干成什么样"翻译成人能看懂的话：**群里一句**，必要时**私聊一份交接单**。

为什么单独一个模块：这些字是主人每天真正会读到的产品界面。写成散落在 ``worker.py``
各处的 f-string，第一次改文案就会有第二份说法，第二次就没人知道哪句是准的。

三条规矩：

* **只在这里定义**：六种结局的群里那句话、拦截后那句"接着干"、成功摘要模板、长度上限；
  ``worker.py`` 只负责把它们交给 outbox（§出口唯一）。
* **说人话**：不出现系统词（借用卡片的 ``BANNED_WORDS`` 同一份表，不复制），
  ``speakable()`` 在测试里当门禁。
* **长度有上限**：群里那条 ≤ ``GROUP_LIMIT``，超了就剪并且**说清完整版在哪**；
  私聊交接单按 ``help.chunk_text`` 那套规则分条（规则只有一处）。
"""
from . import help as help_module
from .approval import BANNED_WORDS

#: 群里那一句的上限：手机上一屏能读完，超了就要剪。
GROUP_LIMIT = 800
#: 私聊交接单单条上限（与说明书同一套分条规则）。
DM_LIMIT = help_module.MAX_CHARS

#: 哪些结局算"卡住了、等人处理"，哪些算"干不成"。与 ``worker.BLOCKED_KINDS`` 同源：
#: 这里只决定"文案里要不要请人回一句执行"，不参与状态判定。
#: 回话端被沙箱拦住 / 判断这是"要动手的活"时该对主人说的那句（提示词与失败路径共用一处）。
ROLE_OFFLOAD_LINE = '这件事得派给干活端，我这就派／要我发卡给你批吗？'

BLOCKED_KINDS = frozenset(('lock_conflict', 'credential_missing', 'cli_unavailable',
                          'workdir', 'policy_violation'))

#: 每种结局**一句话**：主语是"这件活"，不是"系统"。键与 ``WorkResult.kind`` 一致。
KIND_LINES = {
    'timeout': '超时了',
    'no_final': '机器好像睡过去了，我没干完',
    'lock_conflict': '那个会话你正开着，我不敢动',
    'credential_missing': '我这边的登录好像过期了',
    'cli_unavailable': '我这边工具起不来',
    'workdir': '这个活的工作目录开不出来',
    'policy_violation': '有个动作被沙箱规则拦了，活没干下去',
    'error': '干到一半出错了',
}

#: 被拦住时那句"接着干"：统一用已有命令「执行」，不新造词（主人 2026-11-18 定）。
QUESTIONS = {
    'lock_conflict': '把那个会话关掉之后，回我「执行」，我再试一次。',
    'credential_missing': '把登录修好之后，回我「执行」，我再试一次。',
    'cli_unavailable': '把工具修好之后，回我「执行」，我再试一次。',
    'workdir': '把挡着的东西挪开之后，回我「执行」，我再试一次。',
    'policy_violation': '要我做的那件事里有一步被沙箱规矩拦住了；'
                        '你确认要我放行这一步的话，回我「执行」，我按新的做法再试一次。',
}


def line_for(kind):
    """某个结局的一句话。未知结局按 ``error`` 说，不编新说法。"""
    return KIND_LINES.get(kind or '', KIND_LINES['error'])


def question_for(kind):
    """被拦住之后请人回什么。不是"卡住"的结局没有提问。"""
    return QUESTIONS.get(kind or '')


def speakable(body):
    """这句能不能直接发给人看：出现系统词就抛错（测试与调用方共用这一道）。"""
    found = [word for word in BANNED_WORDS if word in body]
    if found:
        raise ValueError('这段文案里有系统词：%s' % '、'.join(found))
    return body.strip()


def _trim(text, limit):
    text = (text or '').strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _size(count):
    """字节数说成人话：1.2 KB / 3.4 MB。产物清单里给人看的是这个。"""
    try:
        count = float(count or 0)
    except (TypeError, ValueError):
        return '大小未知'
    if count < 1024:
        return '%d B' % int(count)
    if count < 1024 * 1024:
        return '%.1f KB' % (count / 1024.0)
    return '%.1f MB' % (count / (1024.0 * 1024.0))


def group_done(*, answer, artifacts=(), duration_seconds=0, space=None):
    """成功：群里那句结论 + **产物在哪、多大**。

    这台机器上**不假设有 git**（主人 2026-11-19 定）：同事机没有 git，所以我们不建工作树、
    不提交，产物就是文件。回执里只报产物路径与大小，**任何地方都不出现提交号**。
    """
    lines = ['这件活干完了。']
    body = (answer or '').strip()
    if body:
        lines += ['', body]
    minutes = max(1, int(round((duration_seconds or 0) / 60.0)))
    if space:
        lines += ['', '干活的目录是 `%s`，用时约 %d 分钟。' % (space, minutes)]
    items = list(artifacts or ())
    if items:
        shown = []
        for item in items[:8]:
            if isinstance(item, dict):
                shown.append('`space/%s`（%s）' % (item.get('path', '?'), _size(item.get('bytes'))))
            else:
                shown.append('`%s`' % item)
        more = '' if len(items) <= 8 else '，另外还有 %d 个没列' % (len(items) - 8)
        lines.append('产物：' + '、'.join(shown) + more)
    else:
        lines.append('跑完了，但没有产出文件。')
    return speakable(_trim('\n'.join(lines), GROUP_LIMIT))


def group_failure(*, kind, last_step=None, dm_sent=False, handoff_path=None):
    """失败：群里那句人话 + 卡在哪 + 完整交接单在哪。"""
    lines = ['这件活没干完：%s%s。' % (line_for(kind),
                                 '，卡在「%s」' % last_step if last_step else '')]
    if dm_sent:
        lines += ['', '完整交接单已经私聊给你：需求原话、做过哪几步、产物在哪、工作空间，'
                      '以及接着跑的命令。']
    elif handoff_path:
        lines += ['', '完整交接单在 `%s`（我这边没找到能给你发私聊的窗口，所以没发出去）。'
                  % handoff_path]
    return speakable(_trim('\n'.join(lines), GROUP_LIMIT))


def group_blocked(*, kind, dm_sent=False, handoff_path=None):
    """被拦住：群里那句人话 + 需要人做的那一件事。"""
    lines = ['这件活我没法动：%s。' % line_for(kind)]
    question = question_for(kind)
    if question:
        lines.append(question)
    if dm_sent:
        lines += ['', '完整交接单已经私聊给你。']
    elif handoff_path:
        lines += ['', '完整交接单在 `%s`（我这边没找到能给你发私聊的窗口，所以没发出去）。'
                  % handoff_path]
    return speakable(_trim('\n'.join(lines), GROUP_LIMIT))


def dm_messages(handoff):
    """私聊那条：交接单全文，按说明书的同一套规则分条（多条时带「（第 i/n 条）」）。"""
    if handoff is None:
        return []
    return [speakable(item) for item in help_module.chunk_text(handoff.as_markdown(), DM_LIMIT)]


def dm_chat_for(store, task):
    """私聊发到哪：**批准这条活的那条私聊消息所在的会话**。

    为什么用它而不是猜一个"主人私聊"：批卡只可能在主人私聊里发生（引用回复/唯一待批/执行
    三条路都要求本人 + 私聊，见 ``bridge/approval.py``）。所以 ``approval_mid`` 指的那条消息
    所在会话，天然就是"能跟他说上话"的那个窗口。
    """
    mid = (task or {}).get('approval_mid')
    if not isinstance(mid, str) or not mid:
        return None
    row = store.message_row(mid)
    chat_id = (row or {}).get('chat_id')
    return chat_id if isinstance(chat_id, str) and chat_id else None


def result_uuids(task_id, attempt):
    """一次结论的幂等键：群一条，私聊一条（多条分条按序号）。

    ``attempt`` 进键里：同一件活重派（``blocked`` 之后回「执行」）会产出新结论，
    不能被当成"上次那条"给幂等掉；而同一次结论重放则必须原样认出来。
    """
    attempt = int(attempt or 1)
    prefix = 'work-result:%s:a%d' % (task_id, attempt)
    return {'group': prefix + ':group', 'dm': prefix + ':dm'}
