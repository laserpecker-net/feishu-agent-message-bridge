"""任务卡与批准判定：**唯一一处**三态解析、拒绝词表、卡片渲染、批准闸门。

为什么集中在一个模块：计划 [plan-s2-tasks-approval.md](../docs/plan-s2-tasks-approval.md) §2.2
要求"三态只在 ``approval.py`` 定义一次，CLI 与 prompt 都不许再判一遍"。同一个判断写两遍，
下一站一定是"模型按旧那份输出、校验按新那份丢字段"——本仓已经踩过（见 ``AGENTS.md`` 第 0 条）。

本模块只做**判断**，不碰网络、不写库、不读配置：

- :func:`render_card` 把五段内容渲染成一条私聊正文（超长按段截断，绝不整块丢弃）；
- :func:`parse_answer` 把主人那句话说成 ``approve`` / ``reject`` / ``ask_again``（**必须含决定词**才算批准，2026-11-17 收紧）；
- :func:`resolve_kind` 把"有没有引用 / 同时几张待批"换成审计用的 ``approval_kind``；
- :func:`judge` 是 PR-3 接线时唯一入口：把一条私聊消息判成"批准哪张卡 / 不成立"；
- :func:`expire_cards` 找出过期没批的卡（调用方负责把"过点了"那句话发出去）。

**「做，但只做①」不是第四态**：它就是 ``approve``，整句原话进 ``hints``，状态值不新增。
"""

import re

from .store import Store

#: 卡片正文上限（计划 §2.1）。超过就按段截断并标注，**不整块丢弃**。
CARD_LIMIT = 800

#: 卡片上三处**由代码写死**的标签（模型只填内容）。顺序固定，见 :func:`render_card`。
CARD_LABEL_PLAN = '我打算：'
CARD_LABEL_TOUCHES = '会动到：'
CARD_LABEL_DONE = '做完你会看到：'

#: 卡片里唯一要主人做的一件事：**写死，模型不能改也不能删**。
#: 2026-11-17 主人反馈「卡片看不懂、不知道要去干嘛」：所以全卡只允许这一句祈使句，
#: 而且必须存在——其余都是背景，读不读都不影响他能不能批。
CARD_ACTION_LINE = '👉 你只要回一个「做」我就开始；回「不做」我就不做了。'

#: 模型填的内容里**不许出现**的系统词。卡片是给不写代码的人看的：
#: 出现这些词，说明模型在替系统说话，而不是替主人说话（同一条反馈）。
BANNED_WORDS = ('账本', '审计', '链路', 'running', 'hints', 'outbox', 'approval',
                'state=', '验收', 'schema', '迁移', '幂等')

#: 步骤编号：最多五步，因为"引用回复逐条批"要能指到具体某一步。
STEP_MARKS = '①②③④⑤'

#: 截断标记。带全角括号，读起来是"这里被剪了"，不是"内容本来如此"。
TRUNCATED = '（已截断）'

#: 每一段被截断后至少保留的字数：宁可整卡超一点点，也不要把某一段剪成一个字。
MIN_SECTION_CHARS = 24

#: 拒绝词表（计划 §2.2）。代码常量，命中任何一个即"他明确说了不做"。
#: 「不批」「不可以」「不行」「驳回」是 2026-11-17 收紧判据时补的：既然批准词里有「批」「可以」「行」，
#: 它们的否定形式必须先被认出来，否则「不批」会被读成「批」。
REJECT_WORDS = ('不做', '不批', '不可以', '不行', '驳回', '先别', '别做', '取消', '等等',
                '不要', '算了', '不办')

#: 批准词：**多字**的词按子串认（「批准了」「同意，但只做①」）。
APPROVE_WORDS = ('批准', '同意', '开工', '可以')

#: 批准词：**单字**的词只在词边界上认（后面不能直接接汉字）。
#: 为什么：单字子串匹配会把「再运行测试」（行）、「改成先备份」（改不属于此表）读成批准——
#: 2026-11-17 主人指出的正是这类含糊句。边界规则让「做」「做，但…」「行吧」「准了」都成立，
#: 而「运行」「执行」「不行」不成立。
APPROVE_TOKENS = ('做', '干', '批', '准', '行', '办', '好')

#: 词边界允许的字符：中文标点、英文标点、空白。单字批准词后面接「了/吧/呢/啊/的/呀」也算。
DECISION_EDGE = '，。！？、,.!?:：;； \t\n' 

#: 三态取值。``ambiguous`` 只出现在 :func:`judge` 里（结构判据不成立，不是他说的三种之一）。
VERDICTS = ('approve', 'reject', 'ask_again')

#: 同一张卡重复追问的最小间隔（秒）。含糊答复只追问一次：连续追问等于刷屏，而主人
#: 已经看到过那句话了。窗口内第二次含糊答复交回正常分流（模型照常聊天），不再重复追问。
ASK_AGAIN_THROTTLE_SECONDS = 10 * 60

#: 卡片有效期（计划 §2.5）：30 分钟。与 ``teach.ASK_TTL_MS`` 同量级，复用同一套"过期就不该再发作答"的直觉。
CARD_TTL_SECONDS = 30 * 60


class CardError(ValueError):
    """起草失败：缺段、超步数、内容为空。调用方据此让任务停在 ``blocked`` 并回群说"我没想清楚"。"""


def _clean(value, limit=None):
    text = ' '.join(str(value or '').split())
    if limit is not None:
        text = text[:limit]
    return text


def _check_plain(text):
    """模型填的内容里出现系统词就报错——报错文案是给人看的，不是给日志看的。"""
    lowered = str(text or '').lower()
    for word in BANNED_WORDS:
        if word.lower() in lowered:
            raise CardError('卡片里有系统词『%s』，请用大白话重写' % word)


def _card_lines(summary, why_me, steps, touches, done_when):
    """把内容排成"按行可截断"的五块，顺手校验缺段与非白话。

    排版（顺序固定，空行位置也固定）::

        第一句：谁让你干什么
        第二句：为什么需要你点头

        我打算：① …
        ② …
        会动到：…

        👉 你只要回一个「做」我就开始；回「不做」我就不做了。

        做完你会看到：…

    为什么按**行**截断：步骤是多行，按段剪会把后面的步骤整条剪掉——那张卡就没法被
    "逐条批"了（计划 §2.1 要求步骤编号可引用）。按行剪，每一步都还在，只是变短。
    """
    summary = _clean(summary)
    why_me = _clean(why_me)
    done_when = _clean(done_when)
    if not summary:
        raise CardError('卡片缺第一句：谁让你干什么。一句话说不清，就不该起草')
    if not why_me:
        raise CardError('卡片缺第二句：为什么需要你点头。说不出会动什么，就不该起草')
    if not done_when:
        raise CardError('卡片缺最后一句：做完你会看到什么。没有这一句，批了也不知道停在哪')
    if isinstance(steps, str):
        steps = [steps]
    steps = [_clean(step) for step in (steps or []) if _clean(step)]
    if len(steps) > len(STEP_MARKS):
        raise CardError('卡片最多 %d 步；再长就该拆成两件事分别批' % len(STEP_MARKS))
    if isinstance(touches, str):
        touches = [touches]
    touches = [_clean(item) for item in (touches or []) if _clean(item)]
    if len(touches) > 3:
        raise CardError('「会动到」最多 3 项：列不下说明你还没想清楚要动哪一块')
    for value in (summary, why_me, ' '.join(steps), ' '.join(touches), done_when):
        _check_plain(value)
    if steps:
        plan = ['%s%s %s' % (CARD_LABEL_PLAN, STEP_MARKS[0], steps[0])]
        plan += ['%s %s' % (STEP_MARKS[index], step)
                 for index, step in enumerate(steps[1:], start=1)]
    else:
        # 还没想好步骤也能批：先让他知道"我会先看清楚"，比留个空段强。
        plan = [CARD_LABEL_PLAN + '我先看清楚再动手']
    touched = CARD_LABEL_TOUCHES + ('；'.join(touches) if touches else '不动任何东西（只读）')
    return [[summary], [why_me], plan, [touched], [CARD_LABEL_DONE + done_when]]


def _compose(sections, trimmed):
    """拼成正文。空行位置固定——这是给人读的卡片，不是给机器解析的报表。"""
    def block(index):
        lines = list(sections[index])
        if trimmed[index]:
            lines.append(TRUNCATED)
        return '\n'.join(lines)

    return '\n'.join([
        block(0),
        block(1),
        '',
        block(2) + '\n' + block(3),
        '',
        CARD_ACTION_LINE,
        '',
        block(4),
    ])


def render_card(*, summary, why_me, steps, touches, done_when):
    """渲染一条任务卡正文：六段固定顺序、总长 ≤ :data:`CARD_LIMIT`。

    超长时**剪当前最长的那一行**（剪完重新判断），被剪过的段追加 :data:`TRUNCATED`。
    任何一行低于 :data:`MIN_SECTION_CHARS` 就停手报错——那说明这张卡本身就写不下，
    应该拆成两件事，而不是把风险那句剪成三个字。

    :data:`CARD_ACTION_LINE` 那行**不参与截断**：它是全卡唯一要主人做的动作，
    哪怕卡被剪到只剩一句话，也必须是这一句。
    """
    sections = _card_lines(summary, why_me, steps, touches, done_when)
    trimmed = [False] * len(sections)
    while True:
        text = _compose(sections, trimmed)
        if len(text) <= CARD_LIMIT:
            return text
        best = None
        for index, lines in enumerate(sections):
            for line_index, line in enumerate(lines):
                if best is None or len(line) > len(sections[best[0]][best[1]]):
                    best = (index, line_index)
        index, line_index = best
        line = sections[index][line_index]
        if len(line) <= MIN_SECTION_CHARS:
            raise CardError('这张卡 %d 字塞不进 %d 字上限：拆成两件事分别批'
                            % (len(text), CARD_LIMIT))
        # 一次只剪一个字、并且总剪当前最长那行：长度会像水位一样摊平，
        # 不会出现"第一句被剪成三个字、最后一段原封不动"。
        sections[index][line_index] = line[:-1].rstrip()
        trimmed[index] = True


def _first_index(text, words):
    found = [text.find(word) for word in words if word in text]
    return min(found) if found else -1


def parse_answer(text):
    """把主人那句话说成三态之一，返回 ``(verdict, hints_text)``。

    判据只有一条：**先表态的那个词算数**。

    - 拒绝词（:data:`REJECT_WORDS`）先出现 → ``reject``；
    - 批准词（:data:`APPROVE_WORDS`）先出现 → ``approve``，整句原话进 ``hints``；
    - 空 / 只有空白 → ``ask_again``（追问一次，不改状态）；
    - **既没有拒绝词也没有决定词**（「我看看」「把第 2 步改成先备份」「预算多少？」）→
      ``ask_again``：不推进、不写 hints，追问一次「要我做吗？回『做』或『不做』」。

    最后这条是 2026-11-17 主人拍板收紧的（原先是"其余任何非空文本都算批准"）：S3a 真开始干活后，
    一句含糊的话会被当成开工指令。**引用只证明"在答这张卡"，不证明"同意做"**。

    「做，但只做①」是批准 + hints；「这个不要做」是拒绝——两种真实说法都由"谁先出现"判对。
    """
    cleaned = ' '.join(str(text or '').split())
    if not cleaned:
        return 'ask_again', ''
    reject_at = _first_index(cleaned, REJECT_WORDS)
    approve_at = _approve_index(cleaned)
    if reject_at != -1 and (approve_at == -1 or reject_at < approve_at):
        return 'reject', cleaned
    if approve_at == -1:
        return 'ask_again', ''
    return 'approve', cleaned


def _approve_index(text):
    """第一个**决定做**的词的位置；没有返回 ``-1``。

    多字词按子串认，单字词按词边界认（见 :data:`APPROVE_TOKENS`）。
    返回位置是为了和拒绝词比"谁先出现"：`做，但不要动生产` = 批准带约束。
    英文 ``ok`` / ``okay`` 也算，但要求两侧不是字母（避免 ``token`` 里的 ok）。
    """
    found = [text.find(word) for word in APPROVE_WORDS if word in text]
    suffix = '(?:了|吧|呢|啊|的|呀)?'
    for word in APPROVE_TOKENS:
        pattern = r'(?:^|[%s])%s%s(?=$|[%s])' % (
            re.escape(DECISION_EDGE), re.escape(word), suffix, re.escape(DECISION_EDGE))
        match = re.search(pattern, text)
        if match:
            found.append(match.start())
    match = re.search(r'(?<![a-z])ok(?:ay)?(?![a-z])', text)
    if match:
        found.append(match.start())
    return min(found) if found else -1


def has_decision_word(text):
    """这句话里有没有**决定词**（批准词或拒绝词）——没有就当"没在答这张卡"。

    用途：没有引用时用"唯一待批"代替代号，前提是主人**真的在说这张卡**。
    随口一句「帮我查下天气」不该被当成批准，也不该被追问一次——交回正常分流。
    """
    cleaned = ' '.join(str(text or '').split())
    if not cleaned:
        return False
    return _first_index(cleaned, REJECT_WORDS) != -1 or _approve_index(cleaned) != -1


def resolve_kind(*, quoted, pending_count):
    """审计用的 ``approval_kind``：``quoted`` / ``unique_pending`` / ``None``（不成立）。

    ``command``（私聊发「执行」）不经过这里：它由 ``Service._run_execute`` 直接调 ``approve_task``。

    为什么允许"没引用"：生产数据（2026-11-17）——私聊里 68 张卡 0 次被引用回复，
    只认引用等于批准永远不发生。唯一性代替引用：**只有一张待批**时「做」没有歧义；
    两张以上就必须引用对号，否则不知道该批哪张。取值只在 :attr:`Store.APPROVAL_KINDS` 定义一次。
    """
    if quoted:
        return Store.APPROVAL_KINDS[0]
    if isinstance(pending_count, int) and pending_count == 1:
        return Store.APPROVAL_KINDS[1]
    return None


def judge(*, text, sender_id, owner_id, chat_type, quoted_mid, card_mid, pending_count,
          sender_is_app=False, owner_ids=None):
    """把一条私聊回复判成"批准哪张卡 / 不成立"。PR-3 接线时只调这一个函数。

    返回 ``{'verdict', 'kind', 'reason', 'hints'}``：``verdict`` 是 :data:`VERDICTS` 之一或
    ``ignore``/``ambiguous``；``kind`` 为 ``None`` 表示不推进（一个字都不许改）。

    四条结构判据，缺一条都不成立（计划 §2.3）：私聊、本人、app 不算、卡要能找到。
    """
    reason = 'ok'
    if sender_is_app:
        return {'verdict': 'ignore', 'kind': None, 'reason': 'app_sender', 'hints': ''}
    if chat_type not in (None, 'p2p'):
        return {'verdict': 'ignore', 'kind': None, 'reason': 'not_private', 'hints': ''}
    owners = {item for item in (owner_ids or ()) if item} or ({owner_id} if owner_id else set())
    if not owners or sender_id not in owners:
        return {'verdict': 'ignore', 'kind': None, 'reason': 'not_owner', 'hints': ''}
    verdict, hints = parse_answer(text)
    if verdict == 'ask_again':
        empty = not ' '.join(str(text or '').split())
        return {'verdict': 'ask_again', 'kind': None,
                'reason': 'empty_answer' if empty else 'no_decision_word', 'hints': ''}
    kind = resolve_kind(quoted=bool(quoted_mid) and quoted_mid == card_mid,
                        pending_count=pending_count)
    if kind is None:
        return {'verdict': 'ambiguous', 'kind': None, 'reason': 'needs_quote', 'hints': hints}
    return {'verdict': verdict, 'kind': kind, 'reason': reason, 'hints': hints}


def is_expired(task, now, ttl_seconds=CARD_TTL_SECONDS):
    """这张卡是否过点了（还没批、且超过有效期）。"""
    if not task or task.get('state') != 'open' or task.get('approval_mid'):
        return False
    try:
        created = float(task.get('created_at') or 0)
    except (TypeError, ValueError):
        return False
    return now - created >= max(0.0, float(ttl_seconds))


def expire_cards(store, now, ttl_seconds=CARD_TTL_SECONDS, announce=None):
    """把过点没批的卡作废（``open → cancelled``），返回作废的 task 列表。

    ``announce(task)`` 由调用方给：把"这张卡过点了"那句话发到群里（本模块不碰发送层）。
    为什么过期要作废而不是留着：私聊里已经堆了 68 张没人处理的卡，留着的下一站就是
    "以后每条新消息都被判成在答某张旧卡"（2026-11-05 事故的同款机制）。
    """
    expired = []
    for task in store.tasks('open', limit=500):
        if not is_expired(task, now, ttl_seconds):
            continue
        cancelled = store.set_task_state(task['id'], 'cancelled', now)
        if cancelled is None:
            continue
        # 作废原因写进步骤账（``cancelled`` 状态本身没有"为什么"这一列）：审计要能区分
        # "主人叫停"（有 approval_mid）与"没人理过期作废"（没有 approval_mid + 这一行）。
        store.note_task(task['id'], 'card_expired：卡片过点没人批，自动作废', now)
        record = dict(task)
        record['state'] = 'cancelled'
        record['expired'] = True
        expired.append(record)
        if announce is not None:
            announce(record)
    return expired
