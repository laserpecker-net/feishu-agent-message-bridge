#!/usr/bin/env python3
"""Shared capability model and local-safety guards for Feishu bridge consumers.

Every consumer entry point (``dsh-consumer.py``, ``codex-consumer.py``) and the
copyable template (``consumer_runtime.py``) use this module, so one review covers
all of them. It is pure Python 3.9 stdlib: no I/O, no state, no subprocess.

Capability model (``CAPABILITIES``): a consumer may *reply*, *retrieve* information
(read files, search, browse the web) and *execute/complete* tasks (run commands,
edit and create files). It may **not** delete files, run destructive commands,
read or disclose passwords/keys/tokens, delegate to sub-agents, or call MCP.

Enforcement has four layers:
  1. the consumer sandbox (DSH per-run overlay, Codex sandbox, Cursor cli.json);
  2. the adapter's stream backstop, which kills the process group as soon as a
     tool event is denied or names a destructive command / secret access;
  3. this module's ``finalize_answer``, applied by the runtime right before the
     answer is submitted to ``events reply``, which replaces key/password
     material with a fixed blocked sentence; ``finalize_reply`` adds the optional
     private escalation (``ask_owner``) and the optional task-ledger update
     (``task``, normalised by ``normalize_task_update``) on top of the same answer;
  4. ``agent_prompt.build_prompt`` (same capability model stated to the model),
     which never counts as the only defence.

Patterns are deliberately conservative: a false positive fails closed (the tool
call is denied / the answer is blocked), never open. Keep them in sync with
``CONTRACTS.md`` and ``CONSUMER-TEMPLATE.md``.
"""
import json
import os
import re
import sys

BLOCKED_REPLY = '这条回复里带有密钥或密码，已拦截，没有发出原文。'
MAX_ANSWER_CHARS = 6000
#: 「这条不用回」的原因上限：一句人话，不是小作文。
MAX_SKIP_REASON_CHARS = 200
#: 私聊求助（`ask_owner`）的问题正文上限：一句话能答的问题不会长；超了就是模型在写作文。
MAX_ASK_CHARS = 1000
#: 求助的类型标签：缺事实 / 缺决定 / 缺权限。只有这三类值得打扰本人。
ESCALATION_NEEDS = ('fact', 'decision', 'access', 'ambiguity', 'uncertainty')
#: 任务台账的状态取值。**只在这里定义一次**：`agent_prompt.task_section` 引用它，
#: 模型看到的允许值和这里校验的值才是同一份。
TASK_STATES = ('open', 'running', 'blocked', 'done', 'failed', 'cancelled')
#: 台账更新的字段上限（见 CONTRACTS.md）：超了就丢那个字段，不丢整条答复。
MAX_TASK_NOTE_CHARS = 500
MAX_TASK_QUESTION_CHARS = 300
MAX_TASK_ARTIFACTS = 20
MAX_TASK_ID_CHARS = 64

CAPABILITIES = {
    'reply': True,
    'retrieve': True,
    'execute': True,
    'delete': False,
    'destructive': False,
    'secret_access': False,
    'secret_disclosure': False,
    'delegate': False,
    'mcp': False,
}

#: 角色 → 能力模型。回话端**不执行**（沙箱 read-only）：它只说话、起草卡，
#: 动东西由干活端做。两个角色的其余边界（不删除、不泄密、不委派、不用 MCP）完全一致。
#: 事实源：CONTRACTS.md §角色定义 的角色定义表。
REPLIER_CAPABILITIES = dict(CAPABILITIES, execute=False)
ROLE_CAPABILITIES = {'replier': REPLIER_CAPABILITIES, 'worker': CAPABILITIES}

# Tool names that are never allowed, after lower-casing and suffix normalisation.
# `mcp__server__tool` style names are covered by the prefix check below.
DENIED_TOOL_NAMES = frozenset({
    'task', 'delete', 'remove', 'mcp', 'subagent', 'subagentfork', 'subagent_fork',
    'workflow', 'collab', 'collabagent', 'spawn', 'spawnagent', 'spawn_agent', 'delegate',
})

COMMAND_KEYS = ('command', 'cmd', 'command_line', 'commandline', 'script',
                'shell_command', 'shellcommand')
PATH_KEYS = ('path', 'file_path', 'filePath', 'target_file', 'targetFile', 'uri',
             'url', 'workdir', 'cwd', 'dir', 'directory')
# Event fields whose value is a tool name (DSH: `tool`; Cursor: `tool_name`; Codex item type).
TOOL_NAME_KEYS = ('tool', 'tool_name', 'toolName', 'name', 'type', 'subtype')

# Commands that remove or destroy data. Checked on command fields, after separators.
# Quotes and parenthesis are separators too: Codex reports `/bin/zsh -lc 'rm ...'`.
_DESTRUCTIVE_COMMAND = re.compile(
    r'(?:^|&&|\|\||[;&|`\n\'"(]|\$\(|\s-exec(?:dir)?\s+)'
    r'\s*(?:(?:sudo|command|exec|nohup|time|nice)\s+)*'
    r'(?:(?:xargs(?:\s+-\S+)*)\s+)?'
    r'(?:env(?:\s+(?:-\S+|[A-Za-z_]\w*=\S+))*\s+)?'
    r'(?:(?:busybox|toybox|coreutils)\s+)?(?:\S*/)?(?:rm|rmdir|unlink|shred|wipe|trash|mkfs|fdisk|dd|truncate)\b'
    r'|diskutil\s+(?:erase|zeroDisk|partitionDisk|secureErase)\w*'
    r'|git\s+clean\b(?![^;&|\n]*\s(?:-n|--dry-run)\b)'
    r'|git\s+reset\s+--hard\b|git\s+checkout\s+--(?:\s|$)'
    r'|git\s+checkout\b[^;&|\n]*\s--\s'
    r'|git\s+checkout\s+-[a-z]*f[a-z]*\s+\.'
    r'|git\s+checkout\s+\.(?:\s|$|["\'`)\]};&|])'
    r'|git\s+(?:stash\s+(?:drop|clear)|branch\s+(?-i:-D)|update-ref\s+-d)\b'
    r'|git\s+restore\b|git\s+rm\b'
    r'|git\s+push\b[^;&|\n]*?(?:-f\b|--force\b)'
    r'|kubectl\s+delete\b|terraform\s+destroy\b|docker\s+rm\b|docker\s+rmi\b|docker\s+volume\s+rm\b'
    r'|find\b[^;&|\n]*\s-delete\b'
    r'|(?:mysql|psql|sqlite3)\b[^;&|\n]*(?:\bdrop\b|\bdelete\s+from\b|\btruncate\b)'
    # Truncating redirects only: `>>`, `2>&1`, `>&2`, `>=`, `->` and `/dev/null` stay allowed.
    r'|&>(?!\s*/dev/null\b)'
    r'|>&\s*(?!/dev/null\b)(?![\d&])'
    r'|(?<![>&|=])(?<!-)(?:[12])?>(?![>&=])(?!\s*/dev/null\b)'
    r'|\bcp\b[^;&|\n]*/dev/null'
    r'|\btee\b[^;&|\n]*<\s*/dev/null'
    # Windows/PowerShell equivalents (native Windows is not a supported bridge host,
    # but a consumer may run commands in WSL2 that still touch a mounted Windows path).
    r'|(?:^|&&|\|\||[;&|\n\'"`(])\s*(?:del|erase)\s+(?:/[a-z]+\s+)*[^\s]'
    r'|\b(?:cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh|wsl(?:\.exe)?)\b[^;&|\n]*?\s(?:del|erase)\s'
    r'|\b(?:rd|rmdir)\s+/s\b'
    r'|\bremove-item\b'
    r'|\bclear-content\b|\bformat-volume\b|\bformat\s+[a-z]:'
    r'|\bdiskpart\b|\bvssadmin\s+delete\b|\breg\s+delete\b'
    r'|\bshutdown\b|\bstop-computer\b|\brestart-computer\b'
    r'|\bcipher\s+/w\b|\bmkfs\.[a-z0-9]+\b',
    re.I)
# Commands whose purpose is to print credentials or the whole environment.
_SECRET_COMMAND = re.compile(
    r'(?:^|&&|\|\||[;&|`\n\'"(]|\$\()'
    r'(?:\s*(?:sudo|command|exec)\s+)*'
    r'(?:env(?:\s+(?:-\S+|[A-Za-z_]\w*=\S+))*\s+)?'
    r'(?:security\s+find-(?:generic|internet)-password\b|printenv\b|env\b(?:\s+(?:-\S+|[A-Za-z_]\w*=\S+))*(?=\s*(?:$|[|>;&\n]))|op\s+read\b|pass\s+show\b'
    r'|cmdkey\s+/list\b|reg\s+query\b[^;&|\n]*(?:password|secret|token)'
    r'|type\s+[^;&|\n]*(?:\.env|id_rsa|id_ed25519|\.pem)\b)',
    re.I)
# Credential file names as shell tokens: `cat .env` has no leading slash, so the
# path regex alone would miss it inside a command string.
_SECRET_COMMAND_PATH = re.compile(
    r'(?:^|[\s\'"=:(])'
    r'(?:\.env(?:\.[\w.-]+)?|\.netrc|\.npmrc|\.pgpass|\.pypirc|\.git-credentials'
    r'|\.htpasswd|id_rsa|id_ed25519|id_ecdsa'
    r'|\.aws/credentials|\.ssh/|\.kube/config|\.docker/config\.json'
    r'|terraform\.tfstate(?:\.[a-z]+)?'
    r'|[^\s/\'"]+\.(?:pem|p12|pfx|key|jks|ppk))'
    r'(?=$|[\s\'"`;&|)])',
    re.I)
# Paths that hold credentials; reading or writing them is denied.
_SECRET_PATH = re.compile(
    r'(?:^|[\\/])(?:'
    r'\.env(?:\.|$|[\\/])'
    r'|\.netrc$|\.npmrc$|\.pgpass$|\.pypirc$|\.git-credentials$|\.htpasswd$'
    r'|id_rsa$|id_ed25519$|id_ecdsa$'
    r'|\.aws[\\/]credentials$|\.ssh[\\/]|keychains?[\\/]'
    r'|\.kube[\\/]config$|\.docker[\\/]config\.json$'
    r'|(?:credentials|secrets?)(?:\.(?:json|ya?ml|toml|ini|cfg))?$'
    r'|[^\\/]+\.(?:pem|p12|pfx|key|keystore|jks|ppk)$'
    r'|terraform\.tfstate(?:\.backup)?$)',
    re.I)
# Material that must never be sent back to Feishu.
_LEAKED_SECRET = re.compile(
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


def require_posix_runtime():
    """Fail fast on hosts this POSIX consumer runtime cannot supervise safely.

    The bridge itself, its flock protocol, process-group cancellation and this
    runtime are POSIX-only (macOS or Linux/WSL2). Native Windows must use the
    HTTP event gateway plus the remote consumer skill instead.
    """
    if os.name != 'posix' or sys.platform == 'win32':
        raise SystemExit(
            '原生 Windows 不支持本消费者的进程/锁模型：请在 WSL2 (Ubuntu) 中运行，'
            '或改用 HTTP 网关 + skills/feishu-bridge-consumer-remote（见 INSTALL-WINDOWS.md）')


def capabilities_report(role='worker'):
    """Plain-data capability model for `--check` output and run receipts."""
    return {'capabilities': dict(ROLE_CAPABILITIES.get(role, CAPABILITIES)),
            'role': role, 'max_answer_chars': MAX_ANSWER_CHARS, 'platform': sys.platform}


def normalize_tool_name(name):
    if not isinstance(name, str):
        return ''
    text = name.strip().lower()
    text = re.sub(r'\(.*\)$', '', text)  # `Delete(**)` / `MCP(server)` style names
    if text.endswith('toolcall'):
        text = text[:-len('toolcall')]
    for prefix in ('tool_',):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
    return text


def denied_tool(name):
    """True for delegation, MCP, and explicit delete tools."""
    text = normalize_tool_name(name)
    if not text:
        return False
    if text in DENIED_TOOL_NAMES:
        return True
    return text.startswith(('mcp', 'subagent', 'spawn', 'collab'))


def command_text(value):
    """Normalise a command field: string, or argv list of strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return ' '.join(value)
    return None


# A commit/tag/merge message may legitimately contain words like "rm" or "delete";
# strip only those argument values before scanning, so `git commit -m "fix rm bug"`
# is allowed while a destructive verb after the message is still caught.
_MESSAGE_ARG = re.compile(
    r'(^|\s)(?:-m|--message)(?:\s+|=)'
    r'(?:[^\s;&|$`()<>"\']+|"[^"$`\\]*"|\'[^\']*\')', re.S)


def _strip_message_args(text):
    return _MESSAGE_ARG.sub(r'\1 -m MSG', text)


#: 截断型重定向（单个 ``>``，会清空目标文件）。回话端一律禁止；干活端在自己的
#: 工作树里跑命令，把输出重定向到日志是正常操作，所以允许（见 ``blocked_command``）。
_TRUNCATING_REDIRECT = re.compile(r'(?<![>&|=])(?<!-)(?:[12])?>(?![>&=])')


def blocked_command(value, allow_truncating_redirect=False):
    """True when a command field removes data or prints credentials.

    ``allow_truncating_redirect``：只放过"单个 ``>`` 重定向"，其余破坏性模式
    （``rm``/``git clean``/``git reset --hard``/``truncate``/``dd`` …）照旧拦。
    为什么要有这个开关：干活端在**一次性工作树**里干活，`cmd > log` 是最普通的写法；
    而回话端没有工作树、删了就没了，所以那边继续全禁。两处用同一个判据、同一个正则，
    只是"这一条规则"按调用方意图开关——不是两套实现。
    """
    text = command_text(value)
    if not text:
        return False
    stripped = _strip_message_args(text)  # message text is data, but only for the destroy scan
    if allow_truncating_redirect:
        # 把截断型重定向换成追加型（`>>`）：只让这一条规则失效，别的破坏性模式不动。
        stripped = _TRUNCATING_REDIRECT.sub(' >> ', stripped)
    return (_DESTRUCTIVE_COMMAND.search(stripped) is not None
            or _SECRET_COMMAND.search(text) is not None
            or _SECRET_PATH.search(text) is not None
            or _SECRET_COMMAND_PATH.search(text) is not None
            or _LEAKED_SECRET.search(text) is not None)


def blocked_path(value):
    """True when a path field points at credentials, or already contains a secret."""
    if not isinstance(value, str):
        return False
    return (_SECRET_PATH.search(value) is not None
            or _LEAKED_SECRET.search(value) is not None)


def leaked_secret(text):
    return isinstance(text, str) and _LEAKED_SECRET.search(text) is not None


def redact_secret_reply(text):
    """Drop outbound text that contains key or password material."""
    return BLOCKED_REPLY if leaked_secret(text) else text


# Answer contract: an agent replies with a JSON object `{"answer": "..."}`. Models
# routinely wrap it in prose ("先看下上下文…{...}") or a ```json fence, and the raw
# reply is what a strict json.loads chokes on. The extractor below stays tolerant
# about the wrapping and strict about the payload: no JSON object, no `answer`
# string, an empty answer, or two different answers are all hard errors.
#
# Cost and exactness both matter: one linear pass matches braces (ignoring braces
# inside JSON strings) and keeps the outermost balanced objects, so no candidate is
# skipped and total work is proportional to the reply length. An unbalanced brace
# makes everything after it unverifiable, so such a reply is rejected rather than
# partly trusted; only the size cap below bounds the scan, never the candidate set.
_MAX_SCAN_CHARS = 262144  # 256 KiB; longer replies must be JSON as a whole.


_MAX_NESTING = 64  # 一层答复对象 + 引用/示例代码；比这更深的只可能是病态输入。


def _exceeds_nesting(text, limit=_MAX_NESTING):
    """括号嵌套是否超过 ``limit``（迭代扫描，不进递归）。

    为什么必须先查这一条：``json.loads`` 对 5000 层嵌套的输入会一路递归到 C 栈耗尽，
    Python 只能抛 ``Fatal Python error: Cannot recover from stack overflow`` 直接 abort
    整个进程（真机复现：整包测试在同一个用例上偶发 SIGABRT，取决于当时栈深度）。
    一句"回复"不可能有 64 层对象，所以先按形状拒绝，再交给 JSON 解析器。
    """
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in '{[':
            depth += 1
            if depth > limit:
                return True
        elif char in '}]':
            depth = max(depth - 1, 0)
    return False


def _object_spans(text):
    """Return spans of the outermost JSON objects in ``text``, or [] if unbalanced."""
    spans, stack = [], []
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if stack:  # Quotes only matter inside a candidate object.
                in_string = True
            continue
        if char == '{':
            stack.append(index)
        elif char == '}' and stack:
            start = stack.pop()
            if not stack:
                # Closing order is document order for outermost objects, since
                # they never overlap; adjacent objects (`}{`) each end one.
                spans.append((start, index + 1))
    return [] if stack else spans


_CONTROL_ESCAPES = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}


def _repair_control_chars(text):
    """Escape control characters that appear inside JSON strings.

    Models routinely write a multi-line Feishu reply as a one-line JSON object with
    **literal** newlines in the string instead of ``\\n``. That is invalid JSON, so
    the whole answer used to be rejected (real case, 2026-11-04: a 900-character
    answer carrying ``ask_owner`` died with "agent reply contains no JSON object").
    The repair only rewrites characters that are illegal inside a string and
    touches nothing outside it, so it cannot turn a non-object into an object.
    """
    if not isinstance(text, str):
        return text
    in_string = escaped = False
    needs_fix = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            elif char < ' ':
                needs_fix = True
                break
        elif char == '"':
            in_string = True
    if not needs_fix:
        return text
    parts, in_string, escaped = [], False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            elif char < ' ':
                parts.append(_CONTROL_ESCAPES.get(char, '\\u%04x' % ord(char)))
                continue
        else:
            if char == '"':
                in_string = True
        parts.append(char)
    return ''.join(parts)


_STRING_OPENERS = '{[,:'
_STRING_CLOSERS = ',}]:'  # 对象的键后面跟 ``:``，值后面跟 ``,``/``}``/``]``


def _repair_unescaped_quotes(text):
    """Escape ASCII quotes that sit *inside* a JSON string value.

    Real failure (2026-11-04): a Chinese answer quoted an English word in plain ASCII
    quotes (``超出"看"的范围``) inside the JSON string, so the whole reply was invalid
    JSON and got dropped. The heuristic reads the shape models actually emit: a quote
    opens a string only right after ``{``/``[``/``,``/``:``, and inside a string it
    closes only when the next non-space character is ``,``/``}``/``]``/``:``; every
    other quote is literal text and gets escaped. Structural characters are never
    touched, so this cannot invent an object that was not there.
    """
    if not isinstance(text, str) or '"' not in text:
        return text
    out, in_string, escaped = [], False, False
    last = ''
    length = len(text)
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
                out.append(char)
            elif char == '\\':
                escaped = True
                out.append(char)
            elif char == '"':
                following = index + 1
                while following < length and text[following] in ' \t\r\n':
                    following += 1
                if following >= length or text[following] in _STRING_CLOSERS:
                    in_string = False
                    out.append(char)
                else:
                    out.append('\\"')
            else:
                out.append(char)
            continue
        if char == '"' and last in _STRING_OPENERS:
            in_string = True
        out.append(char)
        if not char.isspace():
            last = char
    return ''.join(out)


_CONTRACT_START = re.compile(r'^\{\s*"answer"\s*:\s*"')
_ANSWER_MARKER = re.compile(r'"answer"\s*:\s*"')
_QUESTION_MARKER = re.compile(r'"question"\s*:\s*"')
_NEEDS_MARKER = re.compile(r'"needs"\s*:\s*"(fact|decision|access|ambiguity|uncertainty)"')


def _unescape_json_string(raw):
    """把 JSON 字符串内容还原成文本；转义坏了就做最小替换，绝不抛错。"""
    try:
        return json.loads('"' + raw + '"')
    except (ValueError, RecursionError):
        text = raw.replace('\\\\', '\x00')
        for source, target in (('\\n', '\n'), ('\\r', '\r'), ('\\t', '\t'), ('\\"', '"')):
            text = text.replace(source, target)
        return text.replace('\x00', '\\')


def _salvage_reply(text):
    """最后的抢救：坏了但形状还在（``{"answer": "…", "ask_owner": {…}}``）的答复。

    为什么必须有这一步：模型把中文答复写进 JSON 时，偶尔会漏转义内部的双引号，整条 JSON
    就废了。严格的解析器只能判"不是 JSON"→ 本次生成失败 → 提问方什么也收不到（真机
    2026-11-04 连撞三次）。答案优先级高于格式洁癖：只要还能切出正文就发出去，`ask_owner`
    能救回来就一起救，救不回来就只是少一次私聊求助。
    """
    if not isinstance(text, str):
        return None
    # 只有当整段回复就是"我们要求的那一个对象、只是被坏转义拖垮"时才抢救：
    # 引文里夹带的其他对象、不配对的括号、超长回复都必须继续失败关闭（见
    # test_conflicting_answers_fail_closed / test_unbalanced_replies_fail_closed）。
    stripped = text.strip()
    if _CONTRACT_START.match(stripped) is None:
        return None
    if _object_spans(stripped) != [(0, len(stripped))]:
        return None
    text = stripped
    match = _ANSWER_MARKER.search(text)
    if match is None:
        return None
    rest = text[match.end():]
    boundary = rest.rfind('","ask_owner"')
    if boundary >= 0:
        raw_answer, tail = rest[:boundary], rest[boundary:]
    else:
        closing = rest.rfind('"}')
        if closing < 0:
            return None
        raw_answer, tail = rest[:closing], ''
    answer = _unescape_json_string(raw_answer)
    if not answer.strip():
        return None
    value = {'answer': answer}
    question = _QUESTION_MARKER.search(tail or text)
    if question is not None:
        rest = (tail or text)[question.end():]
        boundary = rest.rfind('","needs"')
        raw_question = rest[:boundary] if boundary >= 0 else rest.rsplit('"', 2)[0]
        needs = _NEEDS_MARKER.search(text)
        ask = _unescape_json_string(raw_question).strip()
        if ask and needs is not None:
            value['ask_owner'] = {'question': ask, 'needs': needs.group(1)}
    return value


def parse_answer_json(text):
    """Return the JSON object an agent replied with, tolerating prose and fences.

    The whole reply is tried first; after that each outermost object embedded in
    it is parsed, so a model that explains before answering (or answers and then
    shows a code block) still gets its answer through. Raises ValueError when no
    JSON object can be recovered, or when the reply carries conflicting answers.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('agent reply is empty')
    text = _repair_control_chars(text.strip())
    original = text
    repaired = _repair_unescaped_quotes(text)
    for candidate in (text, repaired):
        if _exceeds_nesting(candidate):
            continue  # 交给 json.loads 会把 C 栈打爆（进程级 abort），只能按形状先拒
        try:
            whole = json.loads(candidate)
        except (ValueError, RecursionError):
            whole = None
        if isinstance(whole, dict):
            return whole
    text = repaired  # 后面按括号扫描时用修好的那份（引号没闭合会把扫描带偏）
    if len(text) > _MAX_SCAN_CHARS:
        raise ValueError('agent reply is too large to scan')
    candidates = []
    for start, end in _object_spans(text):
        if _exceeds_nesting(text[start:end]):
            continue
        try:
            value = json.loads(text[start:end])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        # 抢救时原文和"引号修好"的版本都试：两种修法各自会破坏对方能认的边界。
        for attempt in (original, repaired):
            salvaged = _salvage_reply(attempt)
            if salvaged is not None:
                return salvaged
        raise ValueError('agent reply contains no JSON object')
    answers = [item for item in candidates if isinstance(item.get('answer'), str)]
    if answers:
        # Several different answers in one reply is ambiguous, and ambiguity is
        # exactly how quoted untrusted text could smuggle itself into the reply.
        # Fail closed instead of guessing which object the model meant.
        if len({item['answer'].strip() for item in answers}) > 1:
            raise ValueError('agent reply contains conflicting answers')
        return answers[-1]
    return candidates[-1]


def extract_answer(text):
    """Return the non-empty `answer` string from a tolerant JSON parse."""
    value = parse_answer_json(text)
    answer = value.get('answer')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('agent reply has no non-empty answer')
    return answer.strip()


def finalize_answer(text, limit=MAX_ANSWER_CHARS):
    """Validate, de-secret and bound one outbound answer.

    This is the template-level last line of defence: an adapter bug cannot send
    key material to Feishu through `events reply`.
    """
    if not isinstance(text, str):
        raise ValueError('answer must be text')
    text = text.strip()
    if not text:
        raise ValueError('answer must not be empty')
    if leaked_secret(text):
        return BLOCKED_REPLY
    if len(text) > limit:
        raise ValueError('answer exceeds %d characters' % limit)
    return text


def extract_escalation(value):
    """从同一个 JSON 答复里取可选的 ``ask_owner``（私聊求助）；没有返回 None。

    形状：``{"question": "…", "needs": "fact|decision|access|ambiguity|uncertainty"}``。缺问题、问题为空、
    ``needs`` 不在五类里都是硬错误（含糊的求助比不求助更糟：它会让本人以为已经处理）。
    """
    if not isinstance(value, dict):
        return None
    ask = value.get('ask_owner')
    if ask is None:
        return None
    if not isinstance(ask, dict):
        raise ValueError('ask_owner must be an object')
    question = ask.get('question')
    if not isinstance(question, str) or not question.strip():
        raise ValueError('ask_owner.question must be non-empty text')
    question = ' '.join(question.split())
    if len(question) > MAX_ASK_CHARS:
        raise ValueError('ask_owner.question exceeds %d characters' % MAX_ASK_CHARS)
    needs = ask.get('needs', 'fact')
    if needs not in ESCALATION_NEEDS:
        raise ValueError('ask_owner.needs must be one of: ' + ', '.join(ESCALATION_NEEDS))
    return {'question': question, 'needs': needs}


#: 任务 id 的形状（`tsk_ab12cd34ef56`）。**必须以字母数字开头**：这个值会被拼进
#: `feishu-bridge events reply --task-id` 的 argv，`-` 开头就等于让模型自己加命令行开关。
_TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
#: 产物 sha256 的形状；不是 64 位十六进制就当没写（不猜、不补）。
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
#: 控制字符：台账字段会进 argv（`--task-note`），一个 NUL 就能让 subprocess 抛
#: `embedded null byte`——那样回合一再重试、正文永远发不出去。折行时一并清掉。
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _task_text(value, limit):
    """把台账里的一句话字段折成单行并卡上限；不是非空字符串、超长或含密钥 → None（丢这个字段）。"""
    if not isinstance(value, str):
        return None
    text = _CONTROL_CHARS.sub('', ' '.join(value.split()))
    if not text or len(text) > limit or leaked_secret(text):
        return None
    return text


def _task_artifacts(value):
    """规范化产物列表：允许 ``{"path": …}`` 或裸路径字符串，坏项逐条丢弃，最多 20 条。"""
    if not isinstance(value, list):
        return None
    items = []
    for entry in value:
        if isinstance(entry, str):
            entry = {'path': entry}
        if not isinstance(entry, dict):
            continue
        path = entry.get('path')
        if not isinstance(path, str) or not path.strip():
            continue
        path = _CONTROL_CHARS.sub('', path.strip())
        # 产物路径会随回复发到飞书：指向凭证的路径不能当作"产物"外传。
        if not path or blocked_path(path):
            continue
        item = {'path': path}
        kind = entry.get('kind')
        if isinstance(kind, str) and 0 < len(kind) <= 32:
            item['kind'] = kind
        digest = entry.get('sha256')
        if isinstance(digest, str) and _SHA256.match(digest.lower()):
            item['sha256'] = digest.lower()
        items.append(item)
    return items[:MAX_TASK_ARTIFACTS] or None


#: 模型判出"这是教材"时要落的类型。与 `bridge.teach.KINDS` 同一份事实——但运行时不该 import
#: bridge（消费者是独立进程、只依赖 CLI 契约），所以这里写死后由测试钉住两边一致。
TEACH_KINDS = ('principle', 'style', 'feedback', 'fact', 'pointer', 'lesson')
#: 正文上限比 bridge 的 `teach.MAX_ITEM_CHARS`（500）更松一档是不够的：这里是**先挡一道**，
#: 宁可少收（模型可以改写短一点再发）也不要让一条注定被 bridge 拒掉的记录跑一趟 CLI。
#: 它不是"同一份事实"，所以不钉等于；钉住的是"不许比 bridge 更松"，见测试。
MAX_TEACH_TEXT_CHARS = 400
#: 名称长度上限，与 `bridge.teach.MAX_NAME_CHARS` 同一份事实（测试钉住一致）。
MAX_TEACH_NAME_CHARS = 16


def normalize_teach(value):
    """把答复里的 ``teach`` 规范化成"要不要记条教材"；坏字段直接丢弃（返回 None）。

    模型判错了顶多是"这条没记上"，绝不能因此让本人收不到回复——和 ``normalize_task_update``
    同一条理由：结构化字段是锦上添花，正文是主契约。
    """
    if not isinstance(value, dict):
        return None
    kind = value.get('kind')
    kind = kind.strip().lower() if isinstance(kind, str) else ''
    text = value.get('text')
    text = ' '.join(text.split()) if isinstance(text, str) else ''
    if kind not in TEACH_KINDS or not text or len(text) > MAX_TEACH_TEXT_CHARS:
        return None
    # 名称是给本人看的短标签（「汇报先给结论」）；写坏就丢弃，由 bridge 从正文截取——
    # 没有名字比有一个乱七八糟的名字好，但"没名字"不该让整条教材记不上。
    name = value.get('name')
    if isinstance(name, str):
        name = ' '.join(name.split())
        name = name if 0 < len(name) <= MAX_TEACH_NAME_CHARS else None
    else:
        name = None
    return {'kind': kind, 'text': text, 'name': name}


MAX_TEACH_REVERT_CHARS = 40
_TEACH_REVERT_ID = re.compile(r'^t-\d{4,}$')
#: 名称也能当撤回目标：回执里教本人说的就是「撤回 汇报先给结论」这一句，模型照着回执把名称
#: 塞进 teach_revert 是正常路径。这个值最终作为一个独立 argv 元素交给 CLI（不过 shell），
#: 但仍然要挡住"看起来像选项"和带空白/控制字符的垃圾——那两种只可能是被写坏的输入。
_TEACH_REVERT_UNSAFE = ' \t\r\n-<>&|;`$"' + chr(92)


def _revert_name_ok(candidate):
    if not 0 < len(candidate) <= MAX_TEACH_NAME_CHARS:
        return False
    if '/' in candidate or chr(92) in candidate or '..' in candidate:
        return False  # 看着像路径的一律不认：名称只是用来跟清单里的条目比对的
    return not any(char in _TEACH_REVERT_UNSAFE or ord(char) < 32
                   for char in candidate)


def normalize_teach_revert(value):
    """把答复里的 ``teach_revert`` 规范化成"要撤回哪一条"；坏值直接丢弃（返回 None）。

    为什么撤回也要走结构化字段、不许模型自己敲 ``feishu-bridge teach revert``：写盘只能有一份实现，
    而且消费者的沙箱把 home 目录挡在外面（真机 2026-11-06 撞过：模型自己跑 revert 被
    ``PermissionError`` 拒了，只能在私聊里向本人要权限）。运行时调 CLI 就没有这堵墙。
    值接受条目 id（``t-0007``）、``last``（最后一条）或清单里那条的**名称**（``汇报先给结论``）：
    名称是回执里教本人说的说法，模型照它填进来时必须能生效，否则回执等于教了一句没用的话。
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_TEACH_REVERT_CHARS:
        return None
    if candidate.lower() == 'last' or _TEACH_REVERT_ID.match(candidate.lower()):
        return candidate.lower()
    return candidate if _revert_name_ok(candidate) else None


MAX_REMIND_TEXT_CHARS = 200
MAX_REMIND_NAME_CHARS = 16
MAX_REMIND_AT_CHARS = 40


def normalize_remind(value):
    """把答复里的 ``remind`` 规范成"要他到点提醒"；坏字段整条丢弃返回 None。

    只校验形状（有内容、有绝对时间、长度合理），**不解析时间**：时间读法只有
    ``bridge/remind.to_ms`` 一处实现，这里再解析一遍就是第二套规则。解析失败由 CLI 报
    ``{ok:false,reason}``，运行时写进回执——本人看到的是"没设上，原因是…"。
    """
    if not isinstance(value, dict):
        return None
    text = value.get('text')
    text = ' '.join(text.split()) if isinstance(text, str) else ''
    at = value.get('at')
    at = ' '.join(at.split()) if isinstance(at, str) else ''
    if not text or len(text) > MAX_REMIND_TEXT_CHARS:
        return None
    if not at or len(at) > MAX_REMIND_AT_CHARS:
        return None
    name = value.get('name')
    if isinstance(name, str):
        name = ' '.join(name.split())
        name = name if 0 < len(name) <= MAX_REMIND_NAME_CHARS else None
    else:
        name = None
    return {'at': at, 'text': text, 'name': name}


def normalize_remind_cancel(value):
    """把答复里的 ``remind_cancel`` 规范成"取消哪条提醒"；只收名称或数字编号。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    key = ' '.join(value.split())
    if not key or len(key) > 40:
        return None
    return key


def normalize_task_update(value):
    """把答复里的 ``task`` 规范化成台账更新；坏字段逐项丢弃，返回 None 表示"没有更新"。

    为什么一次只丢一个字段、绝不抛错：台账更新写坏是模型的问题，代价只能是"这一步没记上"，
    不能是"用户连回复也收不到"（正文已经到了 ``finalize_reply`` 的最后一步）。
    """
    if not isinstance(value, dict):
        return None
    update = {}
    task_id = value.get('id')
    if isinstance(task_id, str) and len(task_id) <= MAX_TASK_ID_CHARS and _TASK_ID.match(task_id.strip()):
        update['id'] = task_id.strip()
    state = value.get('state')
    if isinstance(state, str) and state.strip().lower() in TASK_STATES:
        update['state'] = state.strip().lower()
    note = _task_text(value.get('note'), MAX_TASK_NOTE_CHARS)
    if note is not None:
        update['note'] = note
    question = _task_text(value.get('open_question'), MAX_TASK_QUESTION_CHARS)
    if question is not None:
        update['open_question'] = question
    artifacts = _task_artifacts(value.get('artifacts'))
    if artifacts is not None:
        update['artifacts'] = artifacts
    return update or None


#: 任务卡五段的长度上限（字符）。比卡片最终上限（800 字）松一档：这里是**先挡一道**
#: 明显失控的输入（模型把整段聊天记录塞进来），精确截断由 ``bridge/approval.render_card`` 做。
MAX_CARD_SUMMARY = 120
MAX_CARD_WHY = 200
MAX_CARD_STEP = 200
MAX_CARD_TOUCH = 120
MAX_CARD_DONE = 200
MAX_CARD_STEPS = 5
MAX_CARD_TOUCHES = 3


def _card_line(value, limit):
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())[:limit]
    return text or None


def normalize_task_card(value):
    """把答复里的 ``task_card`` 收敛成"能不能起草"；不成立返回 None。

    为什么坏字段返回 None 而不是抛错：起草不成立只是"这件事没请人批"，正文该发还得发——
    让格式问题拦掉回复，等于用一句话换一条消息（本仓的老毛病）。缺段 / 超步数由
    ``approval.render_card`` 再判一次并给出人话原因，这里只挡形状。
    """
    if not isinstance(value, dict):
        return None
    summary = _card_line(value.get('summary'), MAX_CARD_SUMMARY)
    why_me = _card_line(value.get('why_me'), MAX_CARD_WHY)
    done_when = _card_line(value.get('done_when'), MAX_CARD_DONE)
    raw_steps = value.get('steps')
    if isinstance(raw_steps, str):
        raw_steps = [raw_steps]
    if not isinstance(raw_steps, list):
        return None
    steps = [item for item in (_card_line(step, MAX_CARD_STEP) for step in raw_steps) if item]
    raw_touches = value.get('touches')
    if isinstance(raw_touches, str):
        raw_touches = [raw_touches]
    touches = []
    if isinstance(raw_touches, list):
        touches = [item for item in (_card_line(item, MAX_CARD_TOUCH) for item in raw_touches)
                   if item][:MAX_CARD_TOUCHES]
    if not summary or not why_me or not done_when or not steps:
        return None
    if len(steps) > MAX_CARD_STEPS:
        return None
    return {'summary': summary, 'why_me': why_me, 'steps': steps,
            'touches': touches, 'done_when': done_when}


def normalize_direct_work(value):
    """``direct_work`` 的形状校验：与 ``task_card`` 同一套字段，**但多一道硬门槛**。

    本人自己下的写活不需要点头，所以卡片里那句"为什么需要你点头"可以省；可
    ``touches``（要动什么）与 ``done_when``（怎么算做完）不能省——它们决定这活能不能派、
    派出去怎么收尾。缺了就不是"派活失败"，而是"还没想清楚"，运行时据此回一句人话。
    """
    card = normalize_task_card(value)
    if card is None:
        return None
    if not card.get('touches') or not card.get('done_when'):
        return None
    return card


def finalize_reply(text, limit=MAX_ANSWER_CHARS, allow_skip=False):
    """解析并净化一次 Agent 答复，返回
    ``{'answer', 'ask_owner', 'task', 'teach', 'teach_revert', 'remind', 'remind_cancel',
    'skip', 'dropped'}``。

    ``answer`` 与 ``finalize_answer`` 同语义；``ask_owner`` 是"我答不了/要本人拍板"时
    附带的私聊求助，只有问题里有密钥/密码时整条丢弃（不能把密钥问题发给本人，也不该
    因此拦掉正文）。``task`` 是这条消息所在任务的台账更新（见 ``normalize_task_update``），
    没有就返回 None；写坏的字段逐项丢弃，绝不让台账拖垮正文。``dropped`` 记下"字段写了但被丢弃"
    的名字，运行时据此在回复末尾明说"这句没生效"——丢弃本身不能是静默的。

    ``skip`` 是"这条不用回"（一段原因，不发任何消息）：只有群消息允许，由调用方用
    ``allow_skip`` 决定——私聊里对本人沉默等于失联，必须回。给了 skip 又给 answer/ask_owner
    等字段属于自相矛盾，直接报错，不替模型猜哪个才算数。

    不是 JSON 的答复按纯文本处理（旧契约：adapter 自己解析，运行时原样送出）——但正文里
    出现 ``"answer"`` 字段却解析不出来/自相矛盾时仍然失败关闭，否则"含糊的 JSON"会绕过
    上面的歧义检查。
    """
    try:
        value = parse_answer_json(text)
    except ValueError:
        if isinstance(text, str) and '"answer"' not in text:
            return {'answer': finalize_answer(text, limit), 'ask_owner': None, 'task': None,
                    'task_card': None, 'direct_work': None, 'teach': None, 'teach_revert': None,
                    'remind': None, 'remind_cancel': None, 'skip': None, 'dropped': []}
        raise
    skip_reason = value.get('skip')
    if skip_reason is not None:
        if not allow_skip:
            raise ValueError('skip 只允许在群消息里用：私聊对本人沉默等于失联')
        if not isinstance(skip_reason, str) or not skip_reason.strip():
            raise ValueError('skip 必须是一句非空的原因')
        skip_reason = ' '.join(skip_reason.split())
        if len(skip_reason) > MAX_SKIP_REASON_CHARS:
            raise ValueError('skip 原因超过 %d 字' % MAX_SKIP_REASON_CHARS)
        conflicts = [name for name in ('answer', 'ask_owner', 'task', 'task_card', 'direct_work', 'teach',
                                       'teach_revert', 'remind', 'remind_cancel')
                     if value.get(name) not in (None, '', {})]
        if conflicts:
            raise ValueError('skip 和 %s 不能同时出现：回不回只能有一个答案'
                             % '/'.join(conflicts))
        return {'answer': None, 'ask_owner': None, 'task': None, 'task_card': None,
                'teach': None, 'teach_revert': None, 'remind': None, 'remind_cancel': None,
                'skip': skip_reason, 'dropped': []}
    answer = value.get('answer')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('agent reply has no non-empty answer')
    escalation = extract_escalation(value)
    if escalation is not None and leaked_secret(escalation['question']):
        escalation = None
    raw_teach = value.get('teach')
    raw_revert = value.get('teach_revert')
    teach = normalize_teach(raw_teach)
    revert = normalize_teach_revert(raw_revert)
    # 写了但被丢弃 ≠ 没写：运行时要在回复末尾说清"这句没生效"，不能静默丢掉。
    raw_remind = value.get('remind')
    raw_cancel = value.get('remind_cancel')
    remind = normalize_remind(raw_remind)
    cancel = normalize_remind_cancel(raw_cancel)
    dropped = [name for name, raw, kept in (('teach', raw_teach, teach),
                                            ('teach_revert', raw_revert, revert),
                                            ('remind', raw_remind, remind),
                                            ('remind_cancel', raw_cancel, cancel))
               if raw is not None and kept is None]
    raw_card = value.get('task_card')
    card = normalize_task_card(raw_card)
    if raw_card is not None and card is None:
        dropped.append('task_card')
    raw_direct = value.get('direct_work')
    direct = normalize_direct_work(raw_direct)
    if raw_direct is not None and direct is None:
        dropped.append('direct_work')
    if card is not None and direct is not None:
        # 一张卡 + 一个直接派活是自相矛盾的：让他点头还是不问，只能有一个答案。
        dropped.append('direct_work')
        direct = None
    return {'answer': finalize_answer(answer, limit), 'ask_owner': escalation,
            'task': normalize_task_update(value.get('task')), 'task_card': card,
            'direct_work': direct, 'teach': teach, 'teach_revert': revert, 'remind': remind,
            'remind_cancel': cancel, 'skip': None, 'dropped': dropped}


def tool_violation(name, arguments, allow_truncating_redirect=False):
    """Return a short violation reason for one tool call, or None when allowed."""
    if denied_tool(name):
        return 'DeniedTool'
    if isinstance(arguments, dict):
        for key in COMMAND_KEYS:
            if blocked_command(arguments.get(key),
                               allow_truncating_redirect=allow_truncating_redirect):
                return 'DestructiveCommand'
        for key in PATH_KEYS:
            if blocked_path(arguments.get(key)):
                return 'SecretPath'
        return None
    if isinstance(arguments, str) and blocked_command(
            arguments, allow_truncating_redirect=allow_truncating_redirect):
        return 'DestructiveCommand'
    return None


def delete_operation(value):
    """True when an event describes a delete/remove file operation.

    Codex advertises file changes as {type: add|delete|update|move, path: ...};
    the walk keeps this independent of the exact nesting.
    """
    if isinstance(value, dict):
        for key in ('type', 'kind', 'op', 'operation', 'action'):
            if isinstance(value.get(key), str) and value[key].strip().lower() in (
                    'delete', 'remove', 'unlink', 'rmdir', 'rm'):
                return True
        return any(delete_operation(item) for item in value.values())
    if isinstance(value, list):
        return any(delete_operation(item) for item in value)
    return False
