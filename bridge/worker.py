"""干活端：领活、在**独立目录**里干、把结论**送回飞书**并落账。

三条纪律（都有测试钉着）：

* **只领 work**：能领什么由消费者角色决定（``bridge/store.py:ROLE_EVENT_KINDS``），
  这里再显式传 ``kind='work'``，抢错会当场报错而不是空转；
* **每个活一个独立目录**：``<home>/work/<task_id>/space/`` 就是 agent 的工作目录，产物写在里面。
  **不假设机器上有 git**（主人 2026-11-19 定：同事机上没有 git）：不建工作树、不切分支、
  不提交；产物就是文件，回执里报路径与大小。开不出目录才 ``blocked``；
* **"活有结论"与"事件没人处理"是两件事**：活的结论（超时/锁/凭证/工作目录）要 ack 掉事件
  并给任务落状态 + 交接包；只有账本本身写不进去（干线坏了）才走 ``fail_event`` 的重试语义。
"""
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

from . import hold, report, workbench as workbench_module


#: 干活端消费者名（与 launchd 单元同源，见 ``install.py:DSH_WORKER_LABEL``）。
DEFAULT_CONSUMER = 'dsh-worker'
#: 一件活的租约：任务级超时 1800 秒，租约要覆盖"跑完 + 落账"这段，否则会被别人当成过期领走。
DEFAULT_LEASE_SECONDS = 2100
#: 没有活时的轮询间隔。
POLL_SECONDS = 5.0
#: 同时只跑一件活：一台机器一个人，回到常识。
DEFAULT_MAX_CONCURRENCY = 1

#: 认得出"会话被占用"的那句话（真机实测文案，见设计稿 §6.1）。
LOCK_MARKER = 'already owned by an active write handle'

#: 哪些失败算"卡住了、等人处理"（blocked），哪些算"干不成"（failed）。
BLOCKED_KINDS = frozenset(('lock_conflict', 'credential_missing', 'cli_unavailable',
                          'workdir', 'policy_violation'))


class WorkdirError(RuntimeError):
    """开不出这个活的工作目录。``reason`` 是给账本看的短标签，``message`` 是给人看的解释。"""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def space_path(root, task_id):
    """一件活的工作目录：``<root>/<task_id>/space/``。

    ``root`` 是**可配置**的（``worker.work_root``，默认 ``<bridge home>/work``）：
    Windows 上必须指到 ``/mnt/c/…``——WSL ext4 里的目录在 Windows 看来是 UNC，
    ``cmd.exe`` 不接受 UNC 作 cwd（2026-11-20 真机）。
    """
    return Path(root).expanduser() / task_id / 'space'


def prepare_space(root, task_id):
    """建这个活的独立目录（0700）。建不出来就 ``blocked``，别硬干。

    为什么不复用上一次的目录：同一件活重派时人还想看上一次现场，覆盖掉就没证据了。
    所以目录已存在**不报错**（重派续用），只是新产物会和上一次的混在一起——这由回执里的
    产物清单说清楚即可。
    """
    target = space_path(root, task_id)
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(target), 0o700)
    except OSError as error:
        raise WorkdirError('workdir', '这个活的工作目录建不出来：%s（%s）' % (target, error)) from None
    return target


#: 任务包的边界：这几条比"做得漂亮"更重要。**不许出现任何 git 假设**。
PROMPT_CONSTRAINTS = (
    '只做这一件活：不要顺手改别的文件、不要顺手重构、不要做卡片里没要求的事。',
    '要验证就只验证这件活相关的东西；不要跑全量测试（那是主人自己决定的事）。',
    '不要后台执行（不要用 run_in_background 这类方式起长任务）：整件活跑完就结束。',
    # 主人 2026-11-19：同事机上没有 git。不建工作树、不提交，产物就是文件。
    '不要执行任何 git 命令（初始化、暂存、提交都不要）：这台机器可能根本没装 git。',
    '不要访问工作目录以外的东西（不要读别处的文件、不要改系统设置）。',
    '不要探测环境（`env` / `printenv` / `ls ~/.dsh` 这类）、不要尝试提权或绕开沙箱；'
    '被拦住了就把原因写进回复，别硬试。',
)


def build_prompt(requirement, hints, *, space):
    """把任务包变成一段干活说明。**不夹带系统词**：这是给模型看的工作交代。

    requirement 必须是**卡片正文**（主人批准的那张卡），不是群里的来源消息——
    来源消息可能是几分钟前毫不相干的一句话，2026-11-19 真机就因此让模型去干了另一件活。
    """
    lines = ['你在一件独立的工作目录里干活：产物写在这个目录里，做完就结束。', '',
             '## 要干的事', requirement.strip() or '（没有留下原话，按标题理解）']
    extras = [str(item).strip() for item in (hints or []) if str(item).strip()]
    if extras:
        lines += ['', '## 主人批这件活时补充的话'] + ['- %s' % item for item in extras]
    lines += ['', '## 干活的地方',
              '- 工作目录（就是你当前所在的目录）：`%s`' % space,
              '- 要交付的东西都写在这个目录里；外面不要碰', '',
              '## 边界（这几条比「做得漂亮」更重要）']
    lines += ['- %s' % item for item in PROMPT_CONSTRAINTS]
    lines += ['', '## 收尾要求',
              '- 在回复里用三到五行说清：做了什么、产出了哪些文件、怎么验证的、有没有没做完的',
              '- 没做成也要说清卡在哪；不要假装完成']
    return '\n'.join(lines) + '\n'


def machine_name():
    """``dsh-web@<机器名>``：账本里要能回答"这件活是哪台机器干的"。"""
    return socket.gethostname().split('.')[0]


def worker_config(config=None, *, home=None, consumer=None, max_concurrency=None):
    """把 ``worker`` 配置块与显式参数合成一份（缺省值只在这里定义一次）。

    **没有 repo / 工作树配置**：干活端不假设机器上有 git，也就不需要"主仓库"和"工作树放哪"。
    """
    block = dict((config or {}).get('worker') or {})
    home = Path(home or block.get('home') or _default_home()).expanduser()
    #: 每个活的目录根。默认 ``<bridge home>/work``；Windows（cmd-interop）上**必须**配成
    #: ``/mnt/c/Users/<用户>/…``，否则 cmd.exe 会把 cwd 翻成 UNC 并拒绝启动。
    work_root = Path(block.get('work_root') or (home / 'work')).expanduser()
    return {'home': home, 'work_root': work_root,
            'consumer': consumer or block.get('consumer_name') or DEFAULT_CONSUMER,
            'max_concurrency': int(max_concurrency if max_concurrency is not None
                                   else block.get('max_concurrency') or DEFAULT_MAX_CONCURRENCY)}


def read_config(home):
    """读 bridge 的 ``config.json``；读不到/不是对象就返回 ``None``（调用方各自决定失败语义）。"""
    try:
        value = json.loads((Path(home) / 'config.json').read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _default_home():
    return Path(os.environ.get('FEISHU_BRIDGE_HOME')
                or (Path.home() / 'Library/Application Support/FeishuPersonalAgent'))


class Worker:
    """干活端主循环。一次 ``tick()`` 最多干一件活。"""

    def __init__(self, store, config, *, workbench_factory=None, now=time.time,
                 kill_switch=None, prompt_builder=None, raw_config=None):
        self.store = store
        self.config = config
        #: 原始 ``config.json``（``workbench`` 块的唯一读者是 Workbench 自己）。
        self.raw_config = raw_config if raw_config is not None else dict(config or {})
        self.home = Path(config['home'])
        self.work_root = Path(config.get('work_root') or (self.home / 'work'))
        self.consumer = config['consumer']
        self.max_concurrency = max(1, int(config['max_concurrency']))
        self.workbench_factory = workbench_factory or self._default_workbench
        self.now = now
        self.kill_switch = kill_switch
        self.prompt_builder = prompt_builder or build_prompt
        self.busy = False

    # -- 自检 ---------------------------------------------------------------
    def _default_workbench(self, space):
        # work_root 必须显式传 bridge home：默认是 DSH home，运行证据就会写进
        # `~/.dsh/work/…`，而账本 note 指向 bridge home 下的同一个路径（那里是空的）。
        # `allow_truncating_redirect=True`：干活端在自己的独立目录里跑命令，`cmd > log`
        # 是最普通的写法；回话端没有独立目录、删了就没了，那边继续全禁（同一判据、一个开关）。
        return workbench_module.Workbench(self.raw_config, workspace=space, cwd=space,
                                          work_root=self.home, space_root=self.work_root,
                                          allow_truncating_redirect=True, role='worker')

    def prepare(self, workbench=None):
        """启动自检：工作目录根可写、工作台能起。不过就抛异常（不空转）。

        **不检查 git**：这台机器可能根本没有 git（主人 2026-11-19 定），
        所以"是不是 git 仓库"不是干活端的前提。
        """
        root = Path(self.work_root)
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise RuntimeError('工作目录根建不出来：%s（%s）' % (root, error)) from None
        # 用工厂拿探针：真跑用哪个，自检就用哪个（否则自检过的不是将要跑的那个）。
        probe = workbench or self.workbench_factory(root)
        probe.prepare()

    def register(self):
        return self.store.register_consumer(self.consumer, 'dsh', self.now(), role='worker')

    # -- 状态 ---------------------------------------------------------------
    def held(self):
        """主人「下班」时挂起：不再领新活（正在跑的那件由 stop 事件收尾）。"""
        try:
            return bool(hold.read_state(self.home).get('active'))
        except (OSError, ValueError):
            return False  # 读不到就照常干活：挂起文件坏了不该让干活端永久停摆

    def stopping(self):
        return bool(self.kill_switch and self.kill_switch.is_set())

    # -- 主循环 -------------------------------------------------------------
    def tick(self, stop=None):
        """领一件活干完再回来。返回一份"这次发生了什么"的摘要（测试和日志都用它）。"""
        if self.busy:
            return {'skipped': 'busy'}
        if stop is not None and stop.is_set():
            return {'skipped': 'stopping'}
        if self.held():
            return {'skipped': 'held'}
        events = self.store.claim_events(self.consumer, self.now(), limit=1,
                                         lease_seconds=DEFAULT_LEASE_SECONDS, kind='work')
        if not events:
            return {'skipped': 'no_work'}
        event = events[0]
        self.busy = True
        try:
            return self.handle(event, stop)
        finally:
            self.busy = False

    def handle(self, event, stop=None):
        """处理一条已领到的 work 事件：开工、干活、把结论送出去、落账、收尾。

        顺序是合同，不是风格（见模块 docstring 与 ``_conclude``）：

        1. 干活前先看 ``work/<task_id>/result.json``：有，说明上一次**已经干完、只是没来得及
           发出去**——直接走"补发"，绝不重跑（重跑会再开一个工作树，也可能给出不同结论）；
        2. 干完之后先把它写进 ``result.json``（崩溃后仍能补发），再依次 入队 → 落账 → ack。
        """
        claim_token = event.get('claim_token')
        try:
            payload = json.loads(event.get('payload') or '{}')
        except ValueError:
            payload = {}
        task_id = payload.get('task_id') or ''
        task = self.store.get_task(task_id) if task_id else None
        if task is None or task.get('state') != 'running':
            # 任务已经不在 running（作废/被别人结案）：把事件 ack 掉，不硬干。
            self.store.ack_event(self.consumer, event['id'], self.now(), claim_token=claim_token)
            return {'task_id': task_id, 'result': 'stale', 'kind': 'stale'}

        attempt = int(payload.get('attempt') or task.get('attempts') or 1)
        recovered = self._load_result(task_id, attempt)
        if recovered is not None:
            return self._conclude(event, claim_token, task, recovered, recovered=True)

        self.store.set_task_executor(task_id, 'dsh-web@' + machine_name(), self.now())
        requirement = payload.get('requirement') or task.get('title') or ''
        hints = payload.get('hints') or []
        try:
            space = prepare_space(self.work_root, task_id)
        except WorkdirError as error:
            handoff = self._workdir_handoff(task_id, requirement, space_path(self.work_root, task_id),
                                            str(error))
            record = self._record(task, attempt, state='blocked', kind='workdir',
                                  message=str(error), handoff=handoff,
                                  space=str(space_path(self.work_root, task_id)))
            self._persist(task_id, attempt, record)
            summary = self._conclude(event, claim_token, task, record)
            summary['reason'] = error.reason
            return summary

        workbench = self.workbench_factory(space)
        prompt = self.prompt_builder(requirement, hints, space=space)
        result = workbench.run(prompt, task_id=task_id, requirement=requirement,
                               session_id=task_id, stop=stop)
        state = 'done' if result.ok else ('blocked' if result.kind in BLOCKED_KINDS else 'failed')
        kind = result.kind
        handoff, message = result.handoff, (result.text or result.kind)
        artifacts = list(result.artifacts or [])
        if result.ok:
            # 不再有"提交"这一步：这台机器可能没有 git（主人 2026-11-19 定）。
            # 产物就是文件，清单里有相对路径与大小，回执里说人话。
            count = len(artifacts)
            message = ((result.text or '').strip() or '干完了') + (
                '（产出 %d 个文件）' % count if count else '（跑完了，但没有产出文件）')
            if getattr(result, 'artifacts_truncated', False):
                message += '（产物很多，清单只列前 %d 个）' % count
        record = self._record(task, attempt, state=state, kind=kind,
                              message=message, handoff=handoff,
                              space=str(space),
                              answer=result.text if result.ok else '',
                              artifacts=artifacts,
                              duration=result.duration_seconds)
        self._persist(task_id, attempt, record)
        return self._conclude(event, claim_token, task, record)

    # -- 结论的落地与投递 ------------------------------------------------
    def _record(self, task, attempt, *, state, kind, message, handoff,
                space=None, answer='', artifacts=(), duration=0.0):
        """把一次结局整理成一份可落盘、可重发的记录（``result-a<n>.json`` 的形状）。"""
        question = report.question_for(kind) if state == 'blocked' else None
        return {'task_id': task['id'], 'attempt': int(attempt or 1), 'state': state,
                'kind': kind, 'message': (message or '')[:400], 'answer': (answer or '')[:4000],
                'artifacts': list(artifacts or []), 'duration_seconds': float(duration or 0.0),
                'space': space, 'question': question,
                'requirement': (task.get('title') or '')[:200],
                'handoff': None if handoff is None else {
                    'task_id': handoff.task_id, 'requirement': handoff.requirement,
                    'last_step': handoff.last_step,
                    'progress_lines': list(handoff.progress_lines or []),
                    'artifacts': list(handoff.artifacts or []),
                    'workspace': handoff.workspace, 'space': handoff.space,
                    'session_id': handoff.session_id,
                    'resume_command': handoff.resume_command, 'failure': handoff.failure},
                'at': self.now()}

    def _result_path(self, task_id, attempt):
        return self.home / 'work' / task_id / ('result-a%d.json' % int(attempt or 1))

    def _save_result(self, task_id, attempt, record):
        """先落盘再发：进程被杀也不丢"已经干出了什么"，重领时据此补发而不是重跑。

        落不了盘**不是**放弃汇报的理由：结论已经算出来了，人必须听得到。所以这里返回布尔，
        调用方据此在 note 里写明"结论没能落盘"（崩溃恢复能力降级，但结论照发、事件照 ack）。
        反过来若在这里抛出去，事件会被反复重领、主人什么也收不到——那才是静默失败。
        """
        try:
            path = self._result_path(task_id, attempt)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(path))
            return True
        except OSError:
            return False

    def _persist(self, task_id, attempt, record):
        """落盘；失败时把这件事写进 message（人要能看到"这次结论没留下凭据"）。"""
        if self._save_result(task_id, attempt, record):
            return True
        record['message'] = (record.get('message') or '') + '（结论没能落盘，崩溃后无法补发）'
        return False

    def _load_result(self, task_id, attempt):
        try:
            return json.loads(self._result_path(task_id, attempt).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _handoff(self, record):
        raw = record.get('handoff')
        if not isinstance(raw, dict):
            return None
        return workbench_module.Handoff(
            task_id=raw.get('task_id') or record.get('task_id') or '',
            requirement=raw.get('requirement') or '', last_step=raw.get('last_step') or '',
            progress_lines=list(raw.get('progress_lines') or []),
            artifacts=list(raw.get('artifacts') or []), workspace=raw.get('workspace') or '',
            space=raw.get('space'), session_id=raw.get('session_id') or '',
            resume_command=raw.get('resume_command') or '', failure=raw.get('failure') or '')

    def _report(self, task_id, record, *, group_uuid, dm_uuid, dm_chat):
        """把群里那句（必要时还有私聊交接单）排进 outbox。幂等由 uuid 保证，重发不刷屏。"""
        state, kind = record.get('state'), record.get('kind')
        handoff = self._handoff(record)
        handoff_path = 'work/%s/handoff.md' % task_id
        origin_mid = (self.store.get_task(task_id) or {}).get('origin_mid')
        if state == 'done':
            body = report.group_done(answer=record.get('answer'),
                                     artifacts=record.get('artifacts'),
                                     duration_seconds=record.get('duration_seconds'),
                                     space=record.get('space'))
            if origin_mid:
                self.store.publish_say(self.consumer, self.now(), body, group_uuid,
                                       inbox_id=origin_mid)
            else:
                chat_id = (self.store.get_task(task_id) or {}).get('chat_id')
                if chat_id:
                    self.store.publish_say(self.consumer, self.now(), body, group_uuid,
                                           chat_id=chat_id)
            return {'dm_sent': False, 'dm_chat': None}
        dm_sent = bool(dm_chat)
        if state == 'blocked':
            body = report.group_blocked(kind=kind, dm_sent=dm_sent, handoff_path=handoff_path)
        else:
            body = report.group_failure(kind=kind,
                                        last_step=(handoff.last_step if handoff else None),
                                        dm_sent=dm_sent, handoff_path=handoff_path)
        if origin_mid:
            self.store.publish_say(self.consumer, self.now(), body, group_uuid, inbox_id=origin_mid)
        elif (self.store.get_task(task_id) or {}).get('chat_id'):
            self.store.publish_say(self.consumer, self.now(), body, group_uuid,
                                   chat_id=self.store.get_task(task_id)['chat_id'])
        if dm_sent:
            for index, piece in enumerate(report.dm_messages(handoff), 1):
                self.store.publish_say(self.consumer, self.now(), piece,
                                       '%s:%d' % (dm_uuid, index), chat_id=dm_chat)
        return {'dm_sent': dm_sent, 'dm_chat': dm_chat}

    def _conclude(self, event, claim_token, task, record, recovered=False):
        """**① 入队 → ② 落账 → ③ ack**。顺序不能反：

        * 先 ack 再发：进程在中间死掉，结论就永远留在账本里，主人永远看不到；
        * 先发再记：重领会再跑一遍（工作树已存在 → 变 blocked），或者记成"干完了"却没有凭据。

        每次结论的 uuid 带 ``attempt``（``report.result_uuids``）：同一件活被重派后是新结论，
        不能被上一次的幂等键吞掉；同一次结论重放则必须原样认出来。
        """
        task_id = record['task_id']
        attempt = int(record.get('attempt') or 1)
        uuids = report.result_uuids(task_id, attempt)
        dm_chat = report.dm_chat_for(self.store, task)
        try:
            delivery = self._report(task_id, record, group_uuid=uuids['group'],
                                    dm_uuid=uuids['dm'], dm_chat=dm_chat)
        except (LookupError, ValueError, OSError, RuntimeError, sqlite3.Error) as error:
            # ``sqlite3.Error`` 也在这里：账本写不进去时（库锁/磁盘满）同样不能 ack，
            # 否则结论丢了；让它走下面"记 failed + 事件重试"这条降级路径。
            # 话没能排进队列：账不能记成"干完了"，事件也不能 ack——否则主人永远等不到结果。
            # 让事件按既有重试语义回来（30 秒后重领，result.json 还在，直接补发）。
            self.store.record_work_result(task_id, state='failed', now=self.now(),
                                          note='结论没能送到飞书：%s' % type(error).__name__)
            self.store.fail_event(self.consumer, event['id'], self.now(),
                                  'report enqueue failed', retry_delay=30, claim_token=claim_token)
            return {'task_id': task_id, 'result': 'failed', 'kind': 'report_failed'}
        state = record.get('state')
        note = '结论：%s。%s' % (state, (record.get('message') or '').strip()[:300])
        handoff = self._handoff(record)
        if handoff is not None:
            note += '（交接单：work/%s/handoff.md）' % task_id
        if record.get('duration_seconds'):
            note += '用时 %.0f 秒。' % record['duration_seconds']
        self.store.record_work_result(task_id, state=state, now=self.now(), note=note,
                                      artifacts=record.get('artifacts'),
                                      open_question=record.get('question'))
        self.store.ack_event(self.consumer, event['id'], self.now(), claim_token=claim_token)
        return {'task_id': task_id, 'result': state, 'kind': record.get('kind'),
                'attempt': attempt, 'recovered': recovered,
                'dm_sent': delivery.get('dm_sent'),
                'duration_seconds': record.get('duration_seconds')}

    def _workdir_handoff(self, task_id, requirement, space, failure):
        """工作目录开不出来时的交接包：至少让人知道"本来要在哪干活、为什么没干成"。"""
        return workbench_module.Handoff(
            task_id=task_id, requirement=requirement or '', last_step='准备干活',
            progress_lines=['领到活', '准备建工作目录'], artifacts=[],
            workspace=str(space), space=str(space), session_id=task_id,
            resume_command='', failure=failure)

    def run(self, stop=None):
        """常驻循环：没有活就睡一会儿。``stop`` 置位后退。"""
        stop = stop or threading.Event()
        while not (stop.is_set() or self.stopping()):
            try:
                summary = self.tick(stop)
            except (OSError, ValueError, RuntimeError) as error:
                # 干线出错（账本写不进去等）：只记类型，别把堆栈刷进日志；
                # 事件本身的租约会让它在过期后被重新领走。
                summary = {'error': type(error).__name__}
            if summary.get('skipped') and not (stop.is_set() or self.stopping()):
                stop.wait(POLL_SECONDS)
        return True
