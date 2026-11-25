"""干活端执行适配器：**唯一一份** `dsh headless` 运行器。

为什么只有这一份
----------------
在这之前，跑一次 headless 的全部机制都长在 `dsh-consumer.py` 里：0600 的 `--patch`
overlay、NDJSON 事件契约、`start_new_session=True` + 超时杀进程组、2 MiB 输出上限、
凭证路由。S3a 要一个**面向任务**的入口（长跑、会被取消、失败必须交出交接包），
如果另写一份，仓库里就会有两套"怎么跑 headless"的答案——改一处忘一处，线上与测试
各自演化（见 `AGENTS.md` §0.5）。所以这里是把那一套**搬进来**，`dsh-consumer.py`
改成调用本模块，行为逐字不变。

两层的分工
----------
* 本模块的底层（`generate_headless`）只负责"把进程跑起来、把事件读对、把进程树收干净"，
  出错时抛 `RuntimeError`，**消息文本与抽取前完全一致**（consumer 的既有测试就是判据）。
* `Workbench` 是上层：任务级超时、失败分类（`WorkResult.kind`）、以及
  **任何非 `done` 的结局都必须造出一份交接包**（`Handoff`）——只杀不交 = 没做完。
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time


def _paths():
    """延迟导入 ``bridge.platform_paths``：它同时被 CLI/工具直接调用，放模块级最省事。"""
    from bridge import platform_paths

    return platform_paths


def _safety():
    """延迟导入根级 `consumer_safety`：它和 bridge 包一起发布，但只有真正跑 headless 时才需要。"""
    import consumer_safety

    return consumer_safety


#: 沙箱策略**按角色**分（角色定义的唯一事实源：CONTRACTS.md §角色定义）：
#: 回话端只说话——读得到、改不了；干活端才动手——只在它自己的工作目录里写。
#: 为什么必须按角色分而不是靠提示词：回话端两次越界自己动手（2026-11-19 在别的仓库提交、
#: 2026-11-20 把文件写进自己的 workspace），账本里零痕迹。用"自觉"当门禁，迟早失效。
SANDBOX_MODES = {'replier': 'read-only', 'worker': 'workspace-write'}
ROLES = tuple(SANDBOX_MODES)
DEFAULT_ROLE = 'worker'
#: 兼容旧引用：不传角色时的沙箱（= 干活端）。新代码请用 ``sandbox_mode(role)``。
SANDBOX_MODE = SANDBOX_MODES[DEFAULT_ROLE]
APPROVAL_POLICY = 'never'


def sandbox_mode(role=DEFAULT_ROLE):
    """角色 → 沙箱策略。未知角色直接报错：宁可起不来，也不要静默按某个角色跑。"""
    try:
        return SANDBOX_MODES[role]
    except KeyError:
        raise ValueError('未知角色：%r（可选：%s）' % (role, ', '.join(ROLES))) from None
#: 每轮 overlay 关掉的工具行（`dsh headless --patch` 会在 profile 层之后叠加它们）。
DISABLED_TOOLS = ('tool-subagent', 'tool-subagent-fork', 'tool-subagent-control',
                  'tool-subagent-list-agents', 'tool-workflow')
OVERLAY_NAME = 'consumer-overlay.yml'
#: 产物清单最多列这么多个文件（清单给人看，不把整个目录倒进群消息）。
ARTIFACT_LIMIT = 200
#: 可选的**机器级**补丁层，放在用户 DSH home 里（见 CONTRACTS.md §DSH 入口）。
EXTRA_PATCH_NAME = 'feishu-consumer.patch.yml'
#: 可选的**机器级**环境变量文件（同上；典型用途：内部 CA 的 NODE_EXTRA_CA_CERTS）。
EXTRA_ENV_NAME = 'feishu-consumer.env'
#: 这两条是消费者的策略，机器级 env 文件不许覆盖（见 extra_env）。
FORCED_ENV = frozenset({'DSH_HOME', 'DSH_PERMISSION_MODE'})
#: 安装版 headless 可能发出的事件类型；出现别的**直接失败**（不容忍部分成功）。
ALLOWED_EVENTS = frozenset({'session', 'status', 'thinking', 'text', 'tool_call',
                            'tool_result', 'final'})

class _StepBoard:
    """"最近一步"的进程级公告板：给"答得慢时先报一句进度"用。

    为什么是模块级的：进度由解析 NDJSON 的线程（生成线程）写、由定时器线程读，两边不是同一个
    线程，``threading.local`` 读不到。同一个进程里同一时刻只有一次生成（回话端一次回一条），
    所以一块板就够；干活端也用同一份解析器，它只是不读这块板。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._text = None
        self._at = 0.0

    def set(self, text):
        with self._lock:
            self._text, self._at = text, time.monotonic()

    def get(self, default=None):
        with self._lock:
            return self._text or default

    def age(self):
        with self._lock:
            return time.monotonic() - self._at if self._text else None

    def clear(self):
        with self._lock:
            self._text, self._at = None, 0.0


#: 最近一步（`_tool_call` / `_note_step` 写，慢响应回执读）。
STEP_BOARD = _StepBoard()


def note_step(text):
    """记下"现在在干什么"（一行）。只用于给人看的进度，不参与任何判定。"""
    line = ' '.join(str(text or '').split())[:120]
    if line:
        STEP_BOARD.set(line)


def last_step_text(default='等模型回话'):
    """读最近一步；还没有就返回一句人话兜底（定时器线程用）。"""
    return STEP_BOARD.get(default)


#: 回话端一次生成的上限（240 秒）——这是**回话**的预算，不是干活的预算。
CONSUMER_TIMEOUT_SECONDS = 240.0
#: 干活端的默认任务级预算：30 分钟。到点杀进程组，但必须交出交接包。
TASK_TIMEOUT_SECONDS = 1800.0
#: 单次运行的事件流/日志总量上限（stdout 与 stderr 合计）。
DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
#: 超时/取消时给进程组的缓冲时间：SIGTERM 之后多久 SIGKILL。
KILL_GRACE_SECONDS = 5.0
#: 交接包里最多列几行"已经做过的事"。
HANDOFF_PROGRESS_LINES = 10
#: 锁冲突的判定文案（2026-11-17 Windows 开发机实测，见 design-b-loop.md §6.1）。
LOCK_CONFLICT_MARKER = 'already owned by an active write handle'

WORKBENCH_KINDS = ('dsh-web', 'dsh-desktop')

#: 工作台怎么起：``direct`` = 本机直接 exec（mac）；``cmd-interop`` = WSL 里经 ``cmd.exe``
#: 调 Windows 侧的桌面版 CLI（Windows）。两者**只是实现方式不同**，功能必须一致。
LAUNCH_MODES = ('direct', 'cmd-interop')

#: 七种结局。`done` 之外都必须带交接包。
RESULT_KINDS = ('done', 'lock_conflict', 'timeout', 'no_final',
                'credential_missing', 'cli_unavailable', 'error')


class PolicyViolation(RuntimeError):
    """模型想做一件被沙箱规则拦下的事（不是它不会做，是规矩不让）。

    单独一个类型，是因为"能怎么办"完全不同：``error`` 要人排查，``policy_violation``
    要么放行那条规矩、要么把话说清楚让人换个做法——混在一起只会得到一句
    "干到一半出错了"，对谁都没用。
    """

    def __init__(self, reason, *, tool=None, command=None):
        super().__init__('DSH tool call policy violation')
        self.reason = reason or 'Denied'
        self.tool = tool
        self.command = command

    def describe(self):
        bits = ['我想', ('用 `%s` ' % self.tool) if self.tool else '动手',
                ('执行「%s」' % self.command) if self.command else '做一件事',
                '，被沙箱规则拦了（%s）' % self.reason]
        return ''.join(bits)


def _interop_posix_path(raw):
    r"""interop 配置里的路径 → 能在 POSIX 上 stat 的形态。

    `C:\Users\<用户>\.dsh` 要换成 `/mnt/c/Users/<用户>/.dsh`；本来就是 POSIX 形态（测试、
    自定义安装、或有人用 /mnt 写）就原样返回——**换算失败不是错误，是不需要换算**。
    """
    text = str(raw)
    try:
        return Path(_paths().win_to_wsl(_paths().wsl_to_win(text))).expanduser()
    except Exception:  # noqa: BLE001 - 不是 Windows↔WSL 形态：按 POSIX 用
        return Path(text).expanduser()


class WorkbenchError(RuntimeError):
    """适配器自身的问题（配置不对 / CLI 起不来 / 凭证解析不出）。带一个 ``kind``。"""

    def __init__(self, message, kind='error'):
        super().__init__(message)
        self.kind = kind


def overlay_text(credential_route, role=DEFAULT_ROLE):
    """The frozen per-run patch list (see CONTRACTS.md §DSH 入口).

    ``role`` 决定沙箱：回话端 ``read-only``（不写任何东西），干活端 ``workspace-write``。
    其余行（审批策略、禁用工具、凭证三行）两个角色完全一致。
    """
    mode = sandbox_mode(role)
    rows = ['- id: sandbox-policy',
            '  config:',
            '    mode: %s' % mode,
            '- id: approval',
            '  config:',
            '    policy: %s' % APPROVAL_POLICY,
            '- id: permission',
            '  config:',
            '    presets:',
            '      read-only:',
            '        sandbox: read-only',
            '        approval: ask',
            '      workspace-write:',
            '        sandbox: workspace-write',
            '        approval: ask',
            '      danger-full-access:',
            '        sandbox: danger-full-access',
            '        approval: never',
            '      consumer:',
            '        sandbox: %s' % mode,
            '        approval: %s' % APPROVAL_POLICY,
            '    defaultPreset: consumer']
    for tool in DISABLED_TOOLS:
        rows += ['- id: %s' % tool, '  disabled: true']
    if credential_route == 'stored':
        # Headless disables the saved sign-in by default; restore it per run so
        # the user's existing `dsh` credentials work without an env key.
        for row in ('credentials', 'authorization', 'deepseek-account'):
            rows += ['- id: %s' % row, '  disabled: false']
    return '\n'.join(rows) + '\n'


def write_overlay(path, credential_route, role=DEFAULT_ROLE):
    """Write the private overlay 0600 inside this run's directory."""
    text = overlay_text(credential_route, role)
    with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
        file.write(text)
    os.chmod(str(path), 0o600)
    return str(path)


def plain_answer(text):
    """干活端的答案就是**一段人话**，不要求 JSON。

    回话端要的是 ``{"answer": …}``（`consumer_safety` 唯一的解析入口）；干活端的任务包只要求
    "用三到五行说清做了什么"，逼模型包一层 JSON 只会多一种失败方式——2026-11-19 真机就踩到：
    模型老老实实回了人话，解析器报 "agent reply contains no JSON object"，整件活判失败。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('DSH returned an empty answer')
    return text.strip()


def answer_of(text):
    """确认 final 里带着答案，但**原样返回、不做解析**。

    解析、脱敏与长度上限的唯一入口是 `consumer_safety.finalize_reply`；在这里剥掉
    那层 JSON 会丢掉 ``ask_owner``（2026-11-04 真机踩过：私聊升级因此从没触发）。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('DSH returned an empty answer')
    try:
        _safety().extract_answer(text)
    except ValueError as error:
        raise ValueError('Invalid DSH answer: %s' % error) from None
    return text


class RunEvents:
    """Parse the installed headless NDJSON contract, rejecting partial success.

    Every tool call is inspected; a denied one raises after being recorded, so
    the caller can kill the process group and still journal a useful receipt.

    除了"对不对"，它还顺手攒两样只给交接包用的东西：``progress``（做过什么）与
    ``last_step``（卡在哪一步）。它们只读事件流，不影响任何判定。
    """

    def __init__(self, cwd, parse_answer=None, allow_truncating_redirect=False,
                 same_cwd=None):
        self.cwd = str(cwd)
        #: 判定"child 报的 cwd 与我们要的是不是同一个"。``None`` = 严格字符串比较（direct）。
        #: interop 下由 Workbench 注入：child 在 Windows 里跑，报的是 ``C:\\…``，
        #: 我们期望的是 ``/mnt/c/…``——同一个目录的两种写法，必须归一后再比
        #: （2026-11-20 真机：活干完了、文件也写了，却被判 workspace mismatch）。
        self.same_cwd = same_cwd
        #: 干活端在自己的工作树里跑命令，`cmd > log` 属正常写法；回话端不许。
        self.allow_truncating_redirect = bool(allow_truncating_redirect)
        #: ``final`` 文本怎么解析：回话端用 ``answer_of``（要 JSON），干活端用 ``plain_answer``。
        self.parse_answer = parse_answer or answer_of
        self.session_id = None
        self.completed = False
        self.final = None
        self.tool_calls = 0
        self.violations = []
        self.progress = []
        self.last_step = None
        self.error_message = None

    def feed(self, line):
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeError('Invalid DSH event JSON') from None
        if not isinstance(event, dict):
            raise RuntimeError('Invalid DSH event')
        kind = event.get('type')
        if kind == 'error':
            message = event.get('message')
            if isinstance(message, str) and message.strip():
                self.error_message = message.strip()
            raise RuntimeError('DSH reported an error')
        if kind not in ALLOWED_EVENTS:
            raise RuntimeError('Unexpected DSH event')
        if self.final is not None:
            raise RuntimeError('DSH event after final')
        if kind == 'session':
            session_id = event.get('sessionId')
            if self.session_id is not None or not isinstance(session_id, str) or not session_id:
                raise RuntimeError('Invalid DSH session')
            child_cwd = event.get('cwd')
            matched = self.same_cwd(child_cwd) if self.same_cwd is not None else (child_cwd == self.cwd)
            if not matched:
                # 带上两边原值：只有一条 mismatch 而没有原值，排查时等于没有信息。
                raise RuntimeError('DSH session workspace mismatch：child=%s expected=%s'
                                   % (child_cwd, self.cwd))
            self.session_id = session_id
        elif kind == 'status':
            phase = event.get('phase')
            if phase == 'turn_end':
                reason = event.get('reason')
                if self.completed or not isinstance(reason, dict) or reason.get('kind') != 'completed':
                    # 这一轮没跑完：**把 reason 里的真错误捞出来**再抛，否则后面只能去猜
                    # stderr 的最后一行（2026-11-19 真机：交接单里的"为什么停"写成了一行
                    # model listing 日志，人看不出发生过什么）。
                    self._note_reason(reason)
                    raise RuntimeError('DSH turn did not complete')
                self.completed = True
            elif phase not in ('turn_start', 'step_start', 'step_end') or self.completed:
                raise RuntimeError('Invalid DSH status')
            else:
                self._note_step(event)
        elif kind == 'final':
            if not self.session_id or not self.completed or event.get('truncated'):
                self._note_reason(event.get('reason'))
                raise RuntimeError('DSH final arrived without successful turn')
            self.final = self.parse_answer(event.get('text'))
        elif kind == 'tool_call':
            if self.completed:
                raise RuntimeError('Unexpected DSH event')
            self._tool_call(event)
        elif self.completed:
            raise RuntimeError('Unexpected DSH event')
        # thinking / text / tool_result carry no action; their payload stays on
        # disk (0600) and is bounded by max_output_bytes.

    def _note_reason(self, reason):
        """从 ``status.reason`` / ``final.reason`` 里取出真正的错误文案。

        DSH 的失败就写在这两个地方（``reason.error.message``）；拿不到才留给上层去过滤 stderr。
        """
        if self.error_message or not isinstance(reason, dict):
            return
        error = reason.get('error')
        message = error.get('message') if isinstance(error, dict) else error
        if isinstance(message, str) and message.strip():
            self.error_message = message.strip()

    def _note_step(self, event):
        """把 step 的文字记成一行进度（只用于交接包，不参与任何判定）。"""
        text = event.get('text') or event.get('title') or event.get('step')
        if not isinstance(text, str) or not text.strip():
            return
        line = text.strip().splitlines()[0][:120]
        note_step(line)
        if line not in self.progress:
            self.progress.append(line)
            del self.progress[:-HANDOFF_PROGRESS_LINES]

    def _tool_call(self, event):
        safety = _safety()
        tool = event.get('tool')
        if not isinstance(tool, str) or not tool.strip():
            raise RuntimeError('Invalid DSH tool call')
        self.tool_calls += 1
        self.last_step = '调用工具 %s' % tool
        note_step(self.last_step)
        arguments = event.get('input')
        reason = safety.tool_violation(
            tool, arguments, allow_truncating_redirect=self.allow_truncating_redirect)
        if reason is None and safety.delete_operation(arguments):
            reason = 'DeleteOperation'
        if reason is not None:
            self.violations.append(reason)
            command = None
            if isinstance(arguments, dict):
                for key in ('command', 'cmd', 'command_line', 'script'):
                    value = arguments.get(key)
                    if isinstance(value, str) and value.strip():
                        command = value.strip().splitlines()[0][:160]
                        break
            raise PolicyViolation(reason, tool=tool, command=command)


def kill_group(process):
    # Reap an exited wrapper before signaling its group on macOS.
    process.poll()
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            time.sleep(0.15)
            process.poll()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise RuntimeError('DSH process did not terminate') from None


def interop_command(cli, args=()):
    """WSL → Windows 的唯一命令行构造点：``['cmd.exe', '/c', <cli>, *args]``。

    **每个参数各占一个元素，不要自己加引号。** 这是真机实测的结论（2026-11-20，同一台
    Windows 开发机、同一个 cwd）：

    ```
    ['cmd.exe','/c', cli, '--version']                 → rc=0，stdout=['0.2.2-rc.3']   ✅
    ['cmd.exe','/c', '"<cli>" "--version"']            → rc=1，'"C:\\…\\dsh.cmd"' 不是内部或外部命令 ❌
    ```

    WSL 互操作会自己把每个 argv 元素按 Windows 规则加引号；再手写一层引号，
    引号就成了命令的一部分（`"..."` 被当成要执行的程序名）。所以这里只做**分开传参**，
    路径含空格也不用管——互操作已经处理了。

    真正要防的是 **cwd 不能是 UNC**（干活目录必须在 ``/mnt/<盘符>/…`` 上），
    那是另一个判据，见 ``Workbench.prepare``。
    """
    return ['cmd.exe', '/c', str(cli)] + [str(arg) for arg in args]


def taskkill_argv(pid):
    """Windows 侧收进程树：``cmd.exe /c taskkill /T /F /PID <pid>``。

    ``/T`` 连子进程一起杀（``cmd.exe → dsh.cmd → node`` 三层），``/F`` 强制。
    WSL 互操作里 pid 是同一个命名空间，所以直接就能用。
    """
    return interop_command('taskkill', ['/T', '/F', '/PID', str(int(pid))])


def kill_interop(process):
    """cmd-interop 的收尾：**先 Windows 侧 taskkill，再 killpg 兜底**。

    只 killpg 是不够的：Windows 侧的 node 进程不归这个进程组管，会留下孤儿继续烧 token。
    反过来只 taskkill 也不够：WSL 里的 wrapper 可能还在。两边都做，代价是多一条命令。
    """
    process.poll()
    try:
        subprocess.run(taskkill_argv(process.pid), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        kill_group(process)


def generate_headless(binary, overlay, run_dir, workdir, prompt, stop_event, receipt, *,
                      timeout=None, max_output_bytes=None, kill=None, events=None,
                      argv=None, env_overrides=None):
    """跑一次 `dsh headless`，返回 final 的原文；任何不对劲都抛 ``RuntimeError``。

    这是回话端与干活端**共用的唯一实现**。参数全部显式传入（超时、输出上限、收尸函数），
    这样回话端能保住它自己那 240 秒的策略，而不必让本模块猜。
    """
    timeout = CONSUMER_TIMEOUT_SECONDS if timeout is None else timeout
    max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
    kill = kill or kill_group
    events = events or RunEvents(workdir)
    process = None
    deadline = time.monotonic() + timeout
    pending = b''
    total = 0
    # A private anonymous file avoids blocking a stdin pipe before the timeout loop.
    with tempfile.TemporaryFile() as source, selectors.DefaultSelector() as selector, \
         os.fdopen(os.open(str(run_dir / 'stdout.ndjson'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stdout, \
         os.fdopen(os.open(str(run_dir / 'stderr.log'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stderr:
        source.write(prompt.encode('utf-8'))
        source.seek(0)
        environment = dict(os.environ, **extra_env())
        environment['DSH_PERMISSION_MODE'] = 'read-only'
        # cmd-interop：DSH_HOME 等必须按 Windows 形态覆盖给子进程。
        for key, value in (env_overrides or {}).items():
            environment[key] = value
        try:
            if argv is None:
                argv = [str(binary), 'headless', '--patch', str(overlay)] + \
                    extra_patch_args() + ['--json', '-']
            process = subprocess.Popen(argv,
                                       cwd=str(workdir), stdin=source, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=environment,
                                       start_new_session=True)
            for stream, output in ((process.stdout, stdout), (process.stderr, stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, output)
            while selector.get_map() or process.poll() is None:
                if stop_event.is_set():
                    raise RuntimeError('DSH generation cancelled')
                if time.monotonic() >= deadline:
                    raise RuntimeError('DSH generation timed out')
                for key, _mask in selector.select(timeout=0.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_output_bytes:
                        raise RuntimeError('DSH output limit exceeded')
                    key.data.write(chunk)
                    if key.fileobj is process.stdout:
                        pending += chunk
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            events.feed(line)
                if not selector.get_map() and process.poll() is None:
                    stop_event.wait(0.02)
            if pending:
                events.feed(pending)
            if stop_event.is_set():
                raise RuntimeError('DSH generation cancelled')
            if process.returncode != 0:
                raise RuntimeError('DSH process failed')
            if events.final is None:
                raise RuntimeError('DSH final answer is missing')
            return events.final
        finally:
            receipt['session_id'] = events.session_id
            receipt['tool_calls'] = events.tool_calls
            receipt['violations'] = list(events.violations)
            receipt['output_bytes'] = min(total, max_output_bytes)
            if process is not None:
                original_error = sys.exc_info()[0] is not None
                try:
                    kill(process)
                except Exception as error:
                    receipt['cleanup_error'] = type(error).__name__
                    if not original_error:
                        raise RuntimeError('DSH process cleanup failed') from None
                finally:
                    for stream in (process.stdout, process.stderr):
                        stream.close()
                    receipt['exit_code'] = process.returncode


def workspace_from_env():
    """Validate the optional workspace at build time, before any event is claimed."""
    value = os.environ.get('FEISHU_CONSUMER_WORKSPACE') or None
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError('FEISHU_CONSUMER_WORKSPACE must be an absolute path')
    if not path.is_dir():
        raise RuntimeError('FEISHU_CONSUMER_WORKSPACE is not a directory: %s' % path)
    return str(path.resolve())


def dsh_home():
    value = os.environ.get('DSH_HOME')
    return Path(value).expanduser() if value else Path.home() / '.dsh'


def extra_patch_args():
    """Optional machine-level `--patch` layer from the user's DSH home.

    为什么存在：消费者自己的 overlay 由 CONTRACTS 冻结（沙箱/审批/权限/禁用工具），
    但一台接了非官方 provider（例如公司内网 OpenAI 兼容网关）的机器，headless 起不来。
    那属于**机器配置**而不是消费者策略，所以放在用户的 DSH home 里，作为多一层
    `--patch` 传进去（启动器的该参数可重复，叠加在 profile 层之后）。文件不存在时，
    argv 与这个钩子出现之前**逐字节相同**。
    """
    path = dsh_home() / EXTRA_PATCH_NAME
    return ['--patch', str(path)] if path.is_file() else []


def extra_env():
    """Optional machine-level environment for the headless DSH child.

    与 :func:`extra_patch_args` 同理：有些事实是机器本地的，仓库不可能知道。典型例子是
    内网 CA —— macOS Keychain 的信任到不了 Node，必须在进程启动**之前**给 DSH 设
    ``NODE_EXTRA_CA_CERTS``，否则每个请求都 ``SELF_SIGNED_CERT_IN_CHAIN``（见
    ``research/facts-dsh.md`` §2.4/§2.5）。因为这两个变量是启动时读取的，改子进程环境
    是唯一生效点——写在 DSH 的 ``.env`` 里太晚。

    格式：一行一个 ``KEY=VALUE``，空行与 ``#`` 注释忽略。消费者自己的策略键
    (:data:`FORCED_ENV`) 永远不可覆盖。文件不存在 = 子进程环境与今天完全相同。
    """
    path = dsh_home() / EXTRA_ENV_NAME
    values = {}
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        name, separator, value = line.partition('=')
        name = name.strip()
        if not separator or not name or not value.strip() or name in FORCED_ENV:
            continue
        values[name] = value.strip()
    return values


def dotenv_has_key(path):
    """True when the dotenv file assigns a nonempty DEEPSEEK_API_KEY (value ignored)."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        name, separator, value = line.partition('=')
        if separator and name.strip() == 'DEEPSEEK_API_KEY' and value.strip():
            return True
    return False


def credential_route(workspace=None):
    """Pick the headless credential source without ever reading a key value.

    Order follows the DSH provider precedence: launch environment, then the
    managed store (restored per run by the overlay), then a dotenv fallback.
    """
    if (os.environ.get('DEEPSEEK_API_KEY') or '').strip():
        return 'env'
    if (dsh_home() / '.credentials.yaml').is_file():
        return 'stored'
    candidates = [dsh_home() / '.env']
    if workspace:
        candidates.append(Path(workspace) / '.env')
    for candidate in candidates:
        if dotenv_has_key(candidate):
            return 'dotenv'
    return None


#: 明显是"正常日志"的行：DSH 的 stderr 里混着这类，不能拿来当错误给用户看。
LOG_LINE_MARKERS = ('model listing from', 'loaded ', 'skipped ', 'no update available',
                    'Debugger ', 'Inspector ')

#: 明显是"错误"的行：优先拿这些当"为什么停"。
ERROR_LINE_MARKERS = ('error', 'Error', 'ERROR', 'failed', 'Failed', 'exception',
                      'Traceback', 'denied', 'MISSING_CREDENTIAL', 'no API key', 'not permitted')


def looks_like_log(line):
    """这一行看着像正常日志（而不是错误）。"""
    text = str(line or '').strip()
    if not text:
        return True
    if any(marker in text for marker in ERROR_LINE_MARKERS):
        return False
    return text.startswith('[') or any(marker in text for marker in LOG_LINE_MARKERS)


def looks_like_error(line):
    """这一行看着像真错误。"""
    text = str(line or '').strip()
    if not text or looks_like_log(text):
        return False
    return any(marker in text for marker in ERROR_LINE_MARKERS)


def scrub(text, limit=400):
    """错误文案的出口：只留第一句，且不把任何像 key 的东西带出去。"""
    if not isinstance(text, str):
        return ''
    line = ' '.join(text.strip().splitlines()[:1])
    for marker in ('DEEPSEEK_API_KEY=', 'sk-', 'apiKey', 'Bearer '):
        if marker in line:
            line = line.replace(marker, marker.split('=')[0] + '=***')
    return line[:limit]


@dataclass(frozen=True)
class Handoff:
    """一份能让人接着干的交接包。非 ``done`` 的结局里**必须有**。"""

    task_id: str
    requirement: str
    last_step: str
    progress_lines: list
    artifacts: list
    workspace: str
    space: "str | None"
    session_id: str
    resume_command: str
    failure: str

    def as_dict(self):
        return {'task_id': self.task_id, 'requirement': self.requirement,
                'last_step': self.last_step, 'progress_lines': list(self.progress_lines),
                'artifacts': list(self.artifacts), 'workspace': self.workspace,
                'space': self.space, 'session_id': self.session_id,
                'resume_command': self.resume_command, 'failure': self.failure}

    def as_markdown(self):
        lines = ['# 交接单：%s' % self.task_id,
                 '',
                 '**为什么停**：%s' % self.failure,
                 '',
                 '## 需求原话',
                 self.requirement or '（没有留下需求原文）',
                 '',
                 '## 卡在哪一步',
                 self.last_step or '（还没有开始干活）',
                 '',
                 '## 已经做过的事']
        if self.progress_lines:
            lines += ['%d. %s' % (index + 1, line) for index, line in enumerate(self.progress_lines)]
        else:
            lines.append('（还没有可以列出来的步骤）')
        lines += ['', '## 已经产出的文件']
        lines += ['- %s' % item for item in self.artifacts] if self.artifacts else ['（没有产物）']
        lines += ['', '## 工作空间',
                  '- 业务目录：`%s`' % self.workspace]
        if self.space:
            lines.append('- 本次工作目录：`%s`' % self.space)
        lines += ['', '## 接着干', '```bash', self.resume_command or '# 没有可续跑的会话，重新派一次即可', '```']
        # 交接单要自己讲清"这条命令为什么没带那套限制"：无人值守那次是怎么跑的、
        # 人接手时该按什么跑。不写清楚，接手的人会以为命令漏了参数（或反过来被吓住）。
        # 措辞要过 ``report.speakable`` 那道门（交接单会整份发到私聊）：所以只说人话，
        # 不写 policy 名字、不写 approval 这类系统词。
        lines += ['', '> 这次是无人值守跑的：只准改工作目录、不问任何人；',
                  '> 你接手时按自己的习惯跑就行，我故意没把那套限制带进这条命令。']
        return '\n'.join(lines) + '\n'


@dataclass(frozen=True)
class WorkResult:
    ok: bool
    kind: str
    text: str
    session_id: str
    duration_seconds: float
    output_bytes: int
    artifacts: list = field(default_factory=list)
    #: 产物太多、清单被截到 ``ARTIFACT_LIMIT`` 条：**不判失败**，但要让人知道"只列了前 N 个"。
    artifacts_truncated: bool = False
    handoff: "Handoff | None" = None
    work_dir: "str | None" = None


class Workbench:
    """面向一件"活"的执行入口：长跑、可取消、失败必交交接包。

    配置全部显式：``cli`` / ``home`` / ``workspace`` 三个参数优先，其次看
    ``config['workbench']``，最后用缺省值。``cwd`` 是**每次调用**传入的（S3a 里是每个活
    自己的独立目录，见 ``worker.py``），不是配置项。
    """

    def __init__(self, config=None, *, cli=None, home=None, work_root=None, workspace=None,
                 cwd=None, space_root=None, timeout=None, max_output_bytes=None, stop=None,
                 kind=None, allow_truncating_redirect=False, role=None):
        block = dict((config or {}).get('workbench') or {})
        self.config = config or {}
        self.kind = kind or block.get('kind') or 'dsh-web'
        #: 这台执行入口扮演哪个角色：``replier``（只说话）| ``worker``（才动手）。
        #: **必须由调用方显式传**（回话端传 replier、干活端传 worker）；不传按 worker 走，
        #: 保持既有调用面不变，但角色会出现在 describe() 里，肉眼可见。
        self.role = role or block.get('role') or DEFAULT_ROLE
        sandbox_mode(self.role)   # 未知角色立刻失败，不静默跑
        #: 怎么起工作台：``direct``（本机 exec，mac 默认）| ``cmd-interop``（WSL → Windows CLI）
        self.launch = block.get('launch') or 'direct'
        self.cli = Path(cli or block.get('cli') or '').expanduser() if (cli or block.get('cli')) else None
        # 路径一律 resolve：macOS 上 /var 是 /private/var 的符号链接，子进程回报的 cwd
        # 是真实路径，不统一就会把"正常跑完"误判成 workspace mismatch。
        self.home_raw = str(home or block.get('home') or dsh_home())
        #: cmd-interop 下配置里的 home/workspace 是 **Windows 形态**（C:\…），POSIX 上
        #: `Path('C:\\…')` 不是真路径（会 resolve 成 <cwd>/C:\…）——所以这里先归一成
        #: WSL 形态（/mnt/c/…）再落成 Path，后续 is_dir()/os.access() 才有意义。
        self.workspace_raw = str(workspace or block.get('workspace') or (cwd or os.getcwd()))
        if self.launch == 'cmd-interop':
            self.home = _interop_posix_path(self.home_raw)
        else:
            self.home = Path(self.home_raw).expanduser().resolve()
        #: 运行现场（stdout/stderr/交接单/产物）落在哪：**默认 DSH home，但干活端必须显式传
        #: bridge home**——否则证据写进 `~/.dsh/work/…`，而账本里 note 指向 bridge home 下
        #: 的同一个路径，人去看时那里是空的（2026-11-19 真机踩到）。
        self.work_root = Path(work_root or block.get('work_root') or self.home).expanduser().resolve()
        if self.launch == 'cmd-interop':
            self.workspace = _interop_posix_path(self.workspace_raw)
        else:
            self.workspace = Path(self.workspace_raw).expanduser().resolve()
        if cwd and self.launch == 'cmd-interop':
            # interop 下 cwd 要保留 /mnt/<盘符>/… 的**字符串形态**：resolve() 在测试里
            # （把 C: 映射成临时目录）会把 /mnt/c 解析成真实路径，Windows 可见性判定就失真。
            self.cwd = _interop_posix_path(cwd)
        else:
            self.cwd = Path(cwd).expanduser().resolve() if cwd else self.workspace
        #: 干活端把"每个活的目录根"传进来（``worker.work_root``），只用于自检时把
        #: "在 Windows 上能不能看见"这件事说清楚；不参与执行路径。
        self.space_root = (_interop_posix_path(space_root) if space_root
                           else (self.cwd if cwd else self.workspace))
        self.timeout = float(timeout if timeout is not None else block.get('timeout_seconds') or TASK_TIMEOUT_SECONDS)
        self.max_output_bytes = int(max_output_bytes if max_output_bytes is not None
                                     else block.get('max_output_bytes') or DEFAULT_MAX_OUTPUT_BYTES)
        self.credential_overlay = block.get('credential_overlay') or 'auto'
        #: 见 ``consumer_safety.blocked_command``：干活端只多放一条
        #: "截断型重定向"，其余破坏性模式照旧拦。
        self.allow_truncating_redirect = bool(allow_truncating_redirect)
        self.stop = stop
        #: 探针拿到的 CLI 版本（``--version`` 的第一行）；没探到就是 None。
        self.cli_version = None

    # -- 自检 ---------------------------------------------------------------
    def prepare(self):
        """启动自检；不通过抛 :class:`WorkbenchError`（带 ``kind``）。"""
        if self.kind not in WORKBENCH_KINDS:
            raise WorkbenchError('未知的 workbench.kind：%s' % self.kind, 'cli_unavailable')
        if self.launch not in LAUNCH_MODES:
            raise WorkbenchError('未知的 workbench.launch：%s（可选 %s）'
                                 % (self.launch, ' / '.join(LAUNCH_MODES)), 'cli_unavailable')
        if self.kind == 'dsh-desktop' and self.launch != 'cmd-interop':
            raise WorkbenchError('dsh-desktop 必须配 launch=cmd-interop（Windows 桌面版 CLI 要从 '
                                 'WSL 经 cmd.exe 调起）', 'cli_unavailable')
        if self.kind == 'dsh-web' and self.launch == 'cmd-interop':
            raise WorkbenchError('cmd-interop 只能配 kind=dsh-desktop（Windows 桌面版）', 'cli_unavailable')
        if self.launch == 'cmd-interop':
            paths = self._paths_safe()
            if not (paths.looks_like_windows_path(self.home_raw)
                    or paths.looks_like_wsl_path(self.home_raw)):
                raise WorkbenchError(
                    'cmd-interop 下 workbench.home 必须是 Windows 形态（形如 C:\\Users\\<用户>\\.dsh）'
                    '或 /mnt/<盘符>/…；当前是 %s' % self.home_raw, 'cli_unavailable')
        if os.name != 'posix':
            raise WorkbenchError('干活端目前只支持 POSIX（Windows 见 S3b）', 'cli_unavailable')
        binary = self._binary()
        if not binary:
            raise WorkbenchError('找不到 dsh 可执行文件：配置 workbench.cli 或把 dsh 放进 PATH',
                                 'cli_unavailable')
        # 探针：direct 直接 exec；cmd-interop 必须**经 cmd.exe 调 Windows 形态的 CLI**。
        # 真机踩过：interop 下拿 WSL 视角的 `…/dsh.cmd` 去 exec，Linux 直接
        # `[Errno 8] Exec format error`——.cmd 不是 ELF。
        self.cli_version = self._probe_version()
        if self.launch == 'cmd-interop':
            # 运行目录（overlay/证据）也得是 Windows 能读到的位置：提前失败，别跑到第 15 秒才报。
            self._run_root()
        if self.launch == 'cmd-interop':
            env_home = os.environ.get('DSH_HOME')
            if env_home and not self._same_home(env_home):
                raise WorkbenchError('配置的 workbench.home 与环境变量 DSH_HOME 不一致：%s vs %s'
                                     % (self.home_raw, env_home), 'cli_unavailable')
        else:
            env_home = os.environ.get('DSH_HOME')
            if env_home and Path(env_home).expanduser().resolve() != self.home.resolve():
                raise WorkbenchError('配置的 workbench.home 与环境变量 DSH_HOME 不一致：%s vs %s'
                                     % (self.home, env_home), 'cli_unavailable')
        if not self.home.is_dir():
            raise WorkbenchError('workbench.home 不存在：%s' % self._display(self.home),
                                 'cli_unavailable')
        if not self.workspace.is_dir():
            raise WorkbenchError('workbench.workspace 不存在：%s' % self._display(self.workspace),
                                 'cli_unavailable')
        if not os.access(str(self.workspace), os.W_OK):
            raise WorkbenchError('workbench.workspace 不可写：%s' % self._display(self.workspace),
                                 'cli_unavailable')
        if self.launch == 'cmd-interop':
            # 真机 2026-11-20：space 落在 WSL ext4 上时，cmd.exe 会把 cwd 翻成 UNC 并拒绝。
            self._interop_cwd()
        if self.credential_overlay not in ('auto', 'stored', 'env', 'dotenv'):
            raise WorkbenchError('未知的 credential_overlay：%s' % self.credential_overlay, 'cli_unavailable')
        if self.credential_route() is None:
            raise WorkbenchError('没有可用的 headless 凭证：环境里没有 DEEPSEEK_API_KEY，'
                                 '也没有已保存的 dsh 登录', 'credential_missing')

    #: cmd-interop 下 cwd 不可见时的指导文案（真机 2026-11-20：cmd 拒绝 UNC 作 cwd）
    INTEROP_CWD_HINT = ('把 `worker.work_root` 指到 Windows 能看到的位置，'
                        '例如 `/mnt/c/Users/<用户>/feishu-work`')

    def _windows_visible(self, path):
        try:
            return self._paths_safe().is_windows_visible(str(path))
        except Exception:  # noqa: BLE001 - 判定失败按"看不见"处理（宁可报错，不要跑起来才发现）
            return False

    INTEROP_RUN_DIR_HINT = ('把 `worker.work_root` 指到 Windows 能看到的位置，'
                            '例如 `/mnt/c/Users/<用户>/feishu-work`')

    def _windows_target(self, path, what='路径'):
        """**唯一**把路径交给 Windows 的出口：能映射成 ``C:\\…`` 就返回 Windows 形态。

        interop 下凡是进 argv / 环境变量 / cwd 的路径都必须走这里。走不过去就明确报错——
        2026-11-20 真机：overlay 写在 ext4（`/home/…`），`wsl_to_win` 报「不是 WSL 挂载路径」，
        活的第 15 秒就死了。
        """
        paths = self._paths_safe()
        try:
            return paths.wsl_to_win(str(path))
        except Exception:  # noqa: BLE001 - 映射失败必须变成可读错误，不能漏成裸异常
            raise WorkbenchError(
                'cmd-interop 下%s必须在 Windows 能看到的位置（`/mnt/<盘符>/…` ↔ `C:\\…`），'
                '当前是 %s；%s' % (what, self._display(path), self.INTEROP_RUN_DIR_HINT),
                'cli_unavailable') from None

    def _run_root(self):
        """运行现场（overlay / stdout / 交接单）落在哪个根。

        - ``direct``（mac）：``work_root``（默认 bridge home），**布局逐字不变**
        - ``cmd-interop``（Windows）：必须在 Windows 可见的位置，否则 Windows 子进程读不到
          overlay，人也找不到现场。优先用 ``space_root``（= `worker.work_root`），其次 cwd/`work_root`。
        """
        if self.launch != 'cmd-interop':
            return self.work_root
        for candidate in (self.space_root, self.cwd, self.work_root):
            if candidate is not None and self._windows_visible(candidate):
                return candidate
        raise WorkbenchError(
            'cmd-interop 下运行目录必须在 Windows 能看到的位置（`/mnt/<盘符>/…` ↔ `C:\\…`），'
            '当前 space_root=%s、cwd=%s、work_root=%s 都不可见；%s'
            % (self._display(self.space_root), self._display(self.cwd),
               self._display(self.work_root), self.INTEROP_RUN_DIR_HINT),
            'cli_unavailable')

    def _run_dir(self, task_id):
        """一件活的运行目录：interop 放在 Windows 可见盘上（``<root>/<task>/run``）；direct 不变。"""
        if self.launch == 'cmd-interop':
            return self._run_root() / str(task_id) / 'run'
        return self.work_root / 'work' / str(task_id)

    def _interop_cwd(self):
        """interop 下交给 ``cmd.exe`` 的 cwd：**必须是 Windows 能看见的位置**。

        WSL 的 ext4 路径在 Windows 看来是 ``\\wsl.localhost\…`` 的 UNC，而 cmd.exe 不接受
        UNC 作当前目录（2026-11-20 真机：`dsh --version` 退出码 1）。这里不猜、不回退：
        看不见就明确报错并告诉人该改哪个配置。
        """
        if self._windows_visible(self.cwd):
            return self.cwd
        raise WorkbenchError(
            'cmd-interop 的干活目录必须在 Windows 能看到的位置（`/mnt/c/...` ↔ `C:\\...`），'
            '当前是 %s；%s' % (self._display(self.cwd), self.INTEROP_CWD_HINT),
            'cli_unavailable')

    def _probe_cwd(self):
        """版本探针的 cwd：能看见就用 space，看不见就退到 home 的上一级（通常是 Windows 用户目录）。"""
        if not self.launch == 'cmd-interop':
            return None
        if self._windows_visible(self.cwd):
            return self.cwd
        parent = self.home.parent if self._windows_visible(self.home.parent) else None
        if parent is None:
            raise WorkbenchError(
                'cmd-interop 连探针的当前目录都找不到 Windows 可见位置（home=%s）；%s'
                % (self._display(self.home), self.INTEROP_CWD_HINT), 'cli_unavailable')
        return parent

    def _display(self, path):
        """报错里给人看的路径：interop 下换回 Windows 形态（配置里写的就是那个）。"""
        if self.launch != 'cmd-interop':
            return str(path)
        try:
            return self._paths_safe().wsl_to_win(str(path))
        except Exception:  # noqa: BLE001 - 报错信息不能因为换算失败而变没
            return str(path)

    def _same_home(self, other):
        """两个 home 在**同一形态**下比较（interop 归一成 Windows 形态再比）。"""
        paths = self._paths_safe()
        try:
            left = paths.wsl_to_win(self.home_raw).rstrip('\\/').lower()
            right = paths.wsl_to_win(other).rstrip('\\/').lower()
        except Exception:  # noqa: BLE001 - 有一边不是 Windows↔WSL 形态：退回按原样比
            return str(self.home_raw).rstrip('\\/').lower() == str(other).rstrip('\\/').lower()
        return left == right

    def _probe_version(self):
        """跑一次 ``--version`` 拿版本号；失败就是 ``cli_unavailable``（带 cmd 的 stderr 首行）。"""
        if self.launch == 'cmd-interop':
            cli = self._win_cli()
            if not cli:
                raise WorkbenchError('找不到 Windows 侧的 dsh：配置 workbench.cli', 'cli_unavailable')
            probe_argv = interop_command(cli, ['--version'])
        else:
            probe_argv = [self._binary(), '--version']
        probe_cwd = self._probe_cwd()
        try:
            probe = subprocess.run(probe_argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=30, cwd=probe_cwd)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkbenchError('dsh 起不来：%s' % scrub(str(error)), 'cli_unavailable') from None
        if probe.returncode != 0:
            first = (probe.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
            raise WorkbenchError('dsh --version 退出码 %s%s'
                                 % (probe.returncode, ('：' + scrub(first[0][:160])) if first else ''),
                                 'cli_unavailable')
        lines = (probe.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
        return lines[0].strip() if lines else None

    def _binary(self):
        import shutil

        if self.launch == 'cmd-interop':
            # Windows 形态的 CLI：Path('D:\\…') 在 POSIX 上永远不存在，
            # 要用 WSL 视角（/mnt/d/…）判存在；.cmd 没有可执行位，只查存在。
            target = self._win_cli_wsl()
            if target is None:
                return None
            return target if os.path.isfile(target) else None
        if self.cli:
            return str(self.cli) if self.cli.is_file() and os.access(str(self.cli), os.X_OK) else None
        return shutil.which('dsh')

    def _win_cli(self):
        """cmd-interop 用的 CLI 的 **Windows 形态**（传给 cmd.exe 的那个字符串）。"""
        if not self.cli:
            return None
        raw = str(self.cli)
        if self._paths_safe().looks_like_windows_path(raw):
            return raw
        return self._paths_safe().wsl_to_win(raw)

    def _win_cli_wsl(self):
        """同一个 CLI 在 WSL 里的形态（用于存在性检查与 --version 探针）。"""
        win = self._win_cli()
        if win is None:
            return None
        return self._paths_safe().win_to_wsl(win)

    def same_cwd_checker(self):
        """interop 下"child 报的 cwd 与期望是不是同一个目录"的比对器；direct 返回 None。

        两边先归一成 Windows 形态（``wsl_to_win``），再按 Windows 规则比较：
        盘符与整段**不区分大小写**、斜杠方向与尾部斜杠等价。归一失败就算不一致，
        但错误文案里会带上两边原值。
        """
        if self.launch != 'cmd-interop':
            return None

        def _key(raw):
            paths = self._paths_safe()
            text = str(raw)
            win = text if paths.looks_like_windows_path(text) else paths.wsl_to_win(text)
            return win.replace('/', '\\').rstrip('\\').casefold()

        def same(child_cwd):
            if not isinstance(child_cwd, str) or not child_cwd:
                return False
            try:
                return _key(child_cwd) == _key(self.cwd)
            except Exception:  # noqa: BLE001 - child 报了个换算不了的路径 → 就是不一致
                return False

        return same

    def _paths_safe(self):
        return _paths()

    def credential_route(self):
        route = credential_route(str(self.workspace))
        if self.credential_overlay == 'auto':
            return route
        wanted = {'stored': 'stored', 'env': 'env', 'dotenv': 'dotenv'}[self.credential_overlay]
        return route if route == wanted else None

    # -- 干活 ---------------------------------------------------------------
    # -- cmd-interop：同一件事的 Windows 走法 -----------------------------
    def _cmd_interop_argv(self, overlay):
        """``cmd.exe /c <cli> headless --patch <win overlay> --json -``（列表传参，绝不拼字符串）。"""
        cli = self._win_cli()
        if not cli:
            raise WorkbenchError('找不到 Windows 侧的 dsh：配置 workbench.cli'
                                 '（形如 …\\resources\\runtime\\cli\\bin\\dsh.cmd）', 'cli_unavailable')
        paths = self._paths_safe()
        args = ['headless', '--patch', self._windows_target(overlay, 'overlay 路径')]
        for extra in extra_patch_args():
            args.append(paths.wsl_to_win(extra) if paths.looks_like_wsl_path(extra) else extra)
        args += ['--json', '-']
        return interop_command(cli, args)

    def _argv_for(self, overlay):
        if self.launch == 'cmd-interop':
            return self._cmd_interop_argv(overlay), {}
        return None, {}

    def _env_overrides(self):
        if self.launch != 'cmd-interop':
            return {}
        paths = self._paths_safe()
        raw = self.home_raw
        if not (paths.looks_like_windows_path(raw) or paths.looks_like_wsl_path(raw)):
            raise WorkbenchError(
                'cmd-interop 下 workbench.home 必须是 Windows 形态或 /mnt/<盘符>/…；当前是 %s' % raw,
                'cli_unavailable')
        return {'DSH_HOME': paths.wsl_to_win(raw)}

    def _kill_for(self):
        return kill_interop if self.launch == 'cmd-interop' else kill_group

    def _mask(self, text):
        home = str(Path.home())
        text = str(text)
        return '~' + text[len(home):] if home and text.startswith(home) else text

    def describe(self):
        """``workbench check`` 的行：**两端打印同样的行**，只有值不同。"""
        paths = self._paths_safe()
        cli_win = None
        try:
            cli_win = self._win_cli() if self.launch == 'cmd-interop' else (
                str(self.cli) if self.cli else None)
        except Exception:  # noqa: BLE001 - 自检要打印"未知"，不能因此崩
            cli_win = None
        binary_ok = bool(self._binary())
        if self.launch == 'cmd-interop':
            # Windows 形态的 home 在 POSIX 上不能直接 stat；用 /mnt 形态去判它到底在不在。
            display_home = self.home_raw
            try:
                home_ok = Path(paths.win_to_wsl(self.home_raw)).is_dir()
            except Exception:  # noqa: BLE001 - 自检要能打印"不在"，不能因此崩
                home_ok = False
        else:
            display_home = str(self.home)
            home_ok = self.home.is_dir()
        try:
            workspace_ok = self.workspace.is_dir() and os.access(str(self.workspace), os.W_OK)
        except Exception:  # noqa: BLE001 - 自检要能打印"不在/不可写"，不能因此崩
            workspace_ok = False
        rows = [('role', self.role, self.role in ROLES),
                ('launch', self.launch, self.launch in LAUNCH_MODES),
                ('kind', self.kind, self.kind in WORKBENCH_KINDS),
                ('cli', self._mask(cli_win) if cli_win else '(PATH 里的 dsh)', binary_ok),
                ('cli_version', self.cli_version or '未知', bool(self.cli_version)),
                ('home', self._mask(display_home), home_ok),
                ('home_ok', '✓' if home_ok else '✗', home_ok),
                ('workspace', self._mask(str(self._display(self.workspace))), workspace_ok),
                ('workspace_ok', '✓' if workspace_ok else '✗', workspace_ok)]
        if self.launch == 'cmd-interop':
            # 干活目录根必须在 Windows 可见的位置，否则 cmd.exe 连启动都不肯（UNC 不接受）。
            visible = self._windows_visible(self.space_root)
            rows.append(('work_root', self._mask(str(self._display(self.space_root))), visible))
        rows += [
                ('mode', 'dir', True)]
        desktop_home = os.environ.get('DSH_HOME')
        if self.launch == 'cmd-interop':
            same = '未知'
            if desktop_home:
                try:
                    same = '✓' if paths.win_to_wsl(desktop_home).rstrip('/') == \
                        paths.win_to_wsl(self._paths_safe().wsl_to_win(str(self.home))).rstrip('/') else '✗'
                except Exception:  # noqa: BLE001
                    same = '未知'
            rows.append(('same_as_desktop', same, True))
        else:
            same = '✓' if (desktop_home and Path(desktop_home).expanduser().resolve() == self.home) else (
                '未知' if not desktop_home else '✗')
            rows.append(('same_as_desktop', same, True))
        return rows

    def run(self, prompt, *, task_id, requirement, session_id=None, stop=None):
        """跑一件活。**不抛异常**：失败一律落到 ``WorkResult``（并带交接包）。"""
        if not task_id or not isinstance(task_id, str):
            raise ValueError('task_id is required')
        stop = stop or self.stop or threading.Event()
        started = time.monotonic()
        work_dir = self._run_dir(task_id)
        work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(str(work_dir), 0o700)
        except OSError:
            pass
        self._rotate(work_dir)
        events = RunEvents(self.cwd, parse_answer=plain_answer,
                           allow_truncating_redirect=self.allow_truncating_redirect,
                           same_cwd=self.same_cwd_checker())
        receipt = {}
        failure = None
        text = ''
        try:
            self.prepare()
            route = self.credential_route()
            overlay = write_overlay(work_dir / OVERLAY_NAME, route, self.role)
            try:
                argv, _ = self._argv_for(overlay)
                text = generate_headless(self._binary(), overlay, work_dir, self.cwd, prompt,
                                        stop, receipt, timeout=self.timeout,
                                        max_output_bytes=self.max_output_bytes, events=events,
                                        argv=argv, env_overrides=self._env_overrides(),
                                        kill=self._kill_for())
            finally:
                try:
                    os.unlink(overlay)
                except OSError:
                    pass
            kind = 'done'
        except WorkbenchError as error:
            kind, failure = error.kind, scrub(str(error))
        except (FileNotFoundError, PermissionError) as error:
            kind, failure = 'cli_unavailable', scrub(str(error))
        except RuntimeError as error:
            self._last_exit_code = receipt.get('exit_code')
            kind = self._classify(error, events, work_dir)
            failure = self._failure_text(kind, events, work_dir, error)
        except ValueError as error:
            kind, failure = 'error', scrub(str(error)) or 'DSH 的回复不合契约'
        except Exception as error:  # noqa: BLE001 - 任何意外都必须变成"带交接包的失败"
            kind, failure = 'error', scrub('%s: %s' % (type(error).__name__, error))
        duration = time.monotonic() - started
        artifacts, truncated = self._artifacts(self.cwd)
        handoff = None
        if kind != 'done':
            handoff = self._handoff(task_id, requirement, events, artifacts, None,
                                    self.cwd, failure)
            self._write_handoff(work_dir, handoff)
        return WorkResult(ok=(kind == 'done'), kind=kind, text=(text if kind == 'done' else failure or ''),
                          session_id=events.session_id or '', duration_seconds=round(duration, 3),
                          output_bytes=int(receipt.get('output_bytes') or 0),
                          artifacts=artifacts, artifacts_truncated=truncated,
                          handoff=handoff, work_dir=str(work_dir))

    def _rotate(self, work_dir):
        """同一件活重派时，把上一次的现场挪进 attempts/<时间戳>/。

        为什么不让它覆盖：`generate_headless` 用 O_EXCL 建 stdout/stderr，覆盖会把
        "重派"变成 FileExistsError；而直接删掉会把上一次的证据也一起扔掉——重派时
        最需要的恰恰是"上次是怎么死的"。
        """
        names = ('stdout.ndjson', 'stderr.log', 'handoff.md', 'handoff.json', 'overlay.yml')
        existing = [work_dir / name for name in names if (work_dir / name).exists()]
        if not existing:
            return
        stamp = time.strftime('%Y%m%dT%H%M%S')
        target = work_dir / 'attempts' / stamp
        suffix = 0
        while target.exists():
            suffix += 1
            target = work_dir / 'attempts' / ('%s-%d' % (stamp, suffix))
        target.mkdir(parents=True, mode=0o700)
        for path in existing:
            try:
                path.rename(target / path.name)
            except OSError:
                pass

    def _error_evidence(self, events, work_dir):
        """真错误文案：先看事件流，再看 stderr 里像错误的行。"""
        return (events.error_message or '').strip() or (self._stderr_error(work_dir) or '').strip()

    def _classify(self, error, events, work_dir):
        message = str(error)
        evidence = self._error_evidence(events, work_dir)
        tail = self._stderr_tail(work_dir) + ' ' + evidence
        if isinstance(error, PolicyViolation):
            return 'policy_violation'
        if LOCK_CONFLICT_MARKER in tail or LOCK_CONFLICT_MARKER in message:
            return 'lock_conflict'
        if 'timed out' in message or 'cancelled' in message:
            return 'timeout'
        if 'MISSING_CREDENTIAL' in evidence or 'no API key' in evidence:
            return 'credential_missing'
        if 'final answer is missing' in message:
            return 'no_final'
        if 'process failed' in message and events.final is None and not events.error_message:
            # 进程非零退出、也没有 final、也没有 error 事件：会话被中断/机器睡过去/进程被杀。
            # 这在"能怎么办"上和 no_final 是同一类——人去接手，而不是当作代码错误。
            return 'no_final'
        if 'output limit' in message or 'process failed' in message or 'reported an error' in message:
            return 'error'
        return 'error'

    def _stderr_error(self, work_dir, limit=8192):
        """stderr 里最像错误的那一行。

        为什么不能直接取第一行：DSH 的 stderr 混着正常日志（``[llm-deepseek-api-key]
        model listing from … loaded 2 model(s)``），把这种当成"为什么停"给用户看，
        等于什么都没说。优先取含 error/failed/exception/denied/credential 的行，
        取不到再退回第一行非空行。
        """
        tail = self._stderr_tail(work_dir, limit)
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        for line in lines:
            if looks_like_error(line):
                return line
        for line in lines:
            if not looks_like_log(line):
                return line
        return ''

    def _stderr_tail(self, work_dir, limit=4096):
        try:
            with open(os.path.join(str(work_dir), 'stderr.log'), 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - limit))
                return stream.read().decode('utf-8', 'replace')
        except OSError:
            return ''

    def _failure_text(self, kind, events, work_dir, error):
        texts = {'lock_conflict': '这个会话你正开着，我不敢动；关掉我再来',
                 'timeout': '超时了，卡在%s' % (('「%s」' % events.last_step) if events.last_step else '中途'),
                 'no_final': '机器睡过去了，我没干完',
                 'credential_missing': '我这边的登录好像过期了',
                 'cli_unavailable': '我这边工具起不来，先别派活',
                 'policy_violation': '有个动作被沙箱规则拦了'}
        if kind == 'no_final':
            # 退出码是"机器睡过去"和"代码崩了"的唯一切口，有就带上。
            code = getattr(self, '_last_exit_code', None)
            return texts['no_final'] + (('（进程退出码 %s）' % code) if code else '')
        if kind == 'policy_violation':
            violation = error if isinstance(error, PolicyViolation) else None
            if violation is not None:
                return violation.describe()
            return '有个动作被沙箱规则拦了（%s）' % (self._error_evidence(events, work_dir) or '原因不明')
        if kind in texts:
            # 人话在前，真错误在后：前者让人知道怎么办，后者让排查的人有抓手。
            detail = self._error_evidence(events, work_dir)
            return texts[kind] + (('（%s）' % scrub(detail)) if detail else '')
        detail = self._error_evidence(events, work_dir) or str(error)
        return '干到一半出错了：%s' % (scrub(detail) or '（没有更多信息）')

    def _artifacts(self, space, limit=ARTIFACT_LIMIT):
        """产物清单：扫**工作目录本身**，跳过隐藏文件/目录，只报相对路径与字节数。

        上限两条（``ARTIFACT_LIMIT``）：最多列这么多个文件；超过就截断清单——
        清单是给人看的，不是把整个目录倒进群消息。**不因为超限就判失败**。
        """
        found, truncated = [], False
        try:
            for path in sorted(Path(space).rglob('*')):
                if len(found) >= limit:
                    truncated = True
                    break
                relative = path.relative_to(space)
                if any(part.startswith('.') for part in relative.parts):
                    continue
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                found.append({'path': str(relative), 'bytes': int(size)})
        except OSError:
            pass
        return found, truncated

    def _handoff(self, task_id, requirement, events, artifacts, space, cwd, failure):
        session_id = events.session_id or ''
        if session_id:
            resume = 'cd %s && dsh headless --session-id %s --json -' % (
                shlex.quote(str(cwd)), shlex.quote(session_id))
        else:
            resume = 'cd %s && 重新派一次（这次没有建立起会话）' % shlex.quote(str(cwd))
        return Handoff(task_id=task_id, requirement=requirement or '',
                       last_step=events.last_step or (events.progress[-1] if events.progress else ''),
                       progress_lines=list(events.progress), artifacts=list(artifacts),
                       workspace=str(self.workspace), space=str(space) if space else str(cwd),
                       session_id=session_id, resume_command=resume,
                       failure=failure or '未知原因')

    def _write_handoff(self, work_dir, handoff):
        markdown = work_dir / 'handoff.md'
        payload = work_dir / 'handoff.json'
        for path, text in ((markdown, handoff.as_markdown()),
                           (payload, json.dumps(handoff.as_dict(), ensure_ascii=False, indent=2) + '\n')):
            mode = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            with os.fdopen(os.open(str(path), mode, 0o600), 'w') as stream:
                stream.write(text)
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass
