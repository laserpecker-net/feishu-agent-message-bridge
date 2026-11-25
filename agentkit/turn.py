"""``TurnRunner``：**Template Method（模板方法）**。

一次 turn 的骨架固定在这里，顺序不允许被 adapter 改：

1. 校验收参（工作区必须是绝对路径且存在）—— 在任何副作用之前失败；
2. 幂等占位（有 turn_key 时）—— 重复提交直接返回第一次的结果；
3. 组环境（平台层）—— HOME / TMPDIR / PWD 重定向 + 密钥清洗 + UTF-8；
4. 写 prompt 文件（走 stdin 的 CLI）—— 不用 PIPE，避免大 prompt 死锁；
5. 工作区快照（before）—— 用于"产物"判定，不信 agent 自报；
6. 起进程（平台层）—— stdin 卫生与进程组由平台保证；
7. 逐行扫描 —— 解析成事件；工具事件过 deny；输出超限即停；
8. 到点 / 取消 —— 优先让 adapter 用已收集的内容兜一个答案；
9. 快照求差 —— 得出本 turn 的产物；
10. 判定终态 —— adapter 给 status/answer，底座补"诚实字段"（incomplete/ignored/artifacts）；
11. 落盘记录 + 幂等收尾。

为什么是模板方法而不是"每个 adapter 自己写一遍 run"：本仓现有的三个 adapter 各自抄了
一份监督循环（kill 三次、deadline 三次、逐行切分三次）。抄写的代价不是行数，而是
**每修一个坑要修三遍**，而漏掉的那一份只有在线上才暴露。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import artifacts as artifacts_mod
from .adapters.base import AgentAdapter, PrepareContext, TurnState
from .idempotency import TurnStore
from .platform import get_platform
from .platform.stream import EOF, LINE, TIMEOUT, LineReader
from .types import Invocation, Outcome, Reason, TurnRequest, TurnStatus


@dataclass
class TurnConfig:
    """底座的运行参数（与 AGENT 无关）。"""

    #: 单次读取的等待粒度：决定 deadline / 取消的响应速度，不是超时本身
    read_slice_s: float = 0.1
    #: 输出上限（字节）。超过即停，并按 incomplete 收尾
    max_output_bytes: int = 8 * 1024 * 1024
    #: 幂等占位被别人占着时，最多等多久
    wait_for_first_s: float = 120.0
    #: 超过这个时长仍标记 running 的记录视为可接管（进程已死/机器重启）
    stale_after_s: float = 3600.0
    #: 是否做工作区快照（关掉可省 IO，但产物就不可知）
    snapshot: bool = True
    #: >0 时只对"最近这么多秒内变动过的文件"做快照（大工作区必需；0 = 全量）。
    #: 判定语义与代价见 ``artifacts.snapshot(since=...)``：目录级剪枝，原地改老目录里的文件会漏。
    snapshot_window_s: float = 0.0
    #: 快照文件数上限（超大工作区保护）
    snapshot_max_files: int = 20000


def _default_answer_filter(text: str) -> str:
    """默认的答案出站过滤：命中密钥就整条替换成固定文案。

    刻意放在**底座**而不是 adapter：legacy 里每个 adapter 自己调
    ``redact_secret_reply``（``bridge/agent.py:440``、codex ``:236``、dsh ``:110``），
    谁都可能漏；放在出口就只有一处，漏不掉。本仓里它等价于
    ``consumer_safety.redact_secret_reply``；脱离本仓使用时退化为恒等函数。
    """
    try:
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from consumer_safety import redact_secret_reply
    except Exception:  # noqa: BLE001 - 独立使用时没有这个模块，退化为不过滤
        return text
    return redact_secret_reply(text)


class TurnRunner(object):
    """把"一个 adapter + 一个平台"跑成一次 turn。"""

    def __init__(
        self,
        adapter: AgentAdapter,
        platform=None,
        config: Optional[TurnConfig] = None,
        store: Optional[TurnStore] = None,
        answer_filter=None,
    ):
        self.adapter = adapter
        self.platform = platform or get_platform()
        self.config = config or TurnConfig()
        self._store = store
        self._answer_filter = answer_filter or _default_answer_filter

    # ---- 幂等 -----------------------------------------------------------
    def store_for(self, workspace: str) -> TurnStore:
        if self._store is not None:
            return self._store
        paths = self.platform.paths(workspace)
        pid_alive = getattr(self.platform, "pid_alive", None)
        return TurnStore(paths.turns, pid_alive=pid_alive)

    # ---- 主流程 ---------------------------------------------------------
    def run(self, req: TurnRequest, stop_event=None) -> Outcome:
        workspace = self._validate_workspace(req)
        store = self.store_for(workspace) if req.turn_key else None

        if store is not None and req.turn_key:
            begun = store.begin(
                req.turn_key,
                owner=self.adapter.name,
                stale_after_s=self.config.stale_after_s,
            )
            if begun.kind == "replay" and begun.record is not None:
                outcome = store.replay(req.turn_key)
                if outcome is not None:
                    return outcome
            if begun.kind == "busy":
                outcome = store.await_terminal(
                    req.turn_key, timeout_s=self.config.wait_for_first_s
                )
                if outcome is not None:
                    return outcome
                # 等到窗口耗尽仍无终态：交给调用方决定（不在这里跑第二遍）
                from .errors import TurnInProgress

                raise TurnInProgress(req.turn_key, store.path_for(req.turn_key))

        try:
            outcome = self._run_once(req, workspace, stop_event=stop_event)
            outcome = self._maybe_retry_without_resume(req, workspace, outcome, stop_event)
        except Exception as exc:  # noqa: BLE001 - 失败必须落成终态，不能吞成"没跑"
            outcome = Outcome(
                status=TurnStatus.FAILED,
                reason=Reason.SPAWN_FAILED,
                error="%s: %s" % (type(exc).__name__, exc),
                adapter=self.adapter.name,
                turn_key=req.turn_key,
            )
        if store is not None and req.turn_key:
            store.finish(req.turn_key, outcome)
        return outcome

    # ---- 续跑降级 -------------------------------------------------------
    def _maybe_retry_without_resume(
        self, req: TurnRequest, workspace: str, outcome: Outcome, stop_event
    ) -> Outcome:
        """续跑被拒时，用**新会话**再跑一次。

        为什么值得单独做：会话过期/被清理是常态（换机、清了 ``~/.codex``、沙箱回收），
        而对用户来说"这次没答上"是不能接受的 —— 丢掉上下文重来一次，明显好过失败。
        但**不能悄悄做**：重试过的事实必须留在 ``attempts`` 和 ``fallbacks`` 里，
        否则"上下文丢了一次"这件事在日志里就永远查不到。
        """
        if outcome.reason != Reason.RESUME_REJECTED:
            return outcome
        if not req.resume_key or not getattr(req, "allow_resume_fallback", True):
            return outcome
        if not self.adapter.capabilities.supports_resume:
            return outcome
        import dataclasses

        retry_req = dataclasses.replace(req, resume_key=None, turn_key=None)
        retry = self._run_once(retry_req, workspace, stop_event=stop_event)
        retry.fallbacks.append("resume_fallback")
        retry.attempts = outcome.attempts + retry.attempts
        if retry.status != TurnStatus.COMPLETED:
            # 降级也失败：把"为什么本来该续跑"作为诊断信息带上，原始错误优先
            retry.incomplete_reasons.append("resume_fallback_failed")
        return retry

    # ---- 单次执行 -------------------------------------------------------
    def _run_once(self, req: TurnRequest, workspace: str, stop_event=None) -> Outcome:
        cfg = self.config
        paths = self.platform.paths(workspace).ensure()
        run_dir = os.path.join(paths.root, "runs", uuid.uuid4().hex)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        self.platform.harden_path(run_dir, 0o700, is_dir=True)

        binary = self.adapter.resolve_binary(self.platform)
        deadline = time.monotonic() + max(1.0, float(req.timeout_s))
        ctx = PrepareContext(
            workspace=workspace,
            run_dir=run_dir,
            agentkit_home=paths.home,
            agentkit_tmp=paths.tmp,
            binary=binary,
            deadline=deadline,
            platform=self.platform,
        )
        self.adapter.preflight(req, ctx)

        # 窗口起点只算一次：before/after 必须用同一个基准，否则差的语义会被自己搅乱
        snapshot_since = (
            time.time() - cfg.snapshot_window_s
            if cfg.snapshot and cfg.snapshot_window_s > 0
            else None
        )
        before = (
            artifacts_mod.snapshot(workspace, max_files=cfg.snapshot_max_files,
                                   since=snapshot_since)
            if cfg.snapshot
            else None
        )

        argv = list(self.adapter.build_argv(req, ctx))
        env = self.platform.prepare_env(
            workspace=workspace,
            extra=self.adapter.env_patch(req, ctx),
            allow_secrets=self.adapter.allow_secret_env(req),
            home_mode=getattr(self.adapter, "home_policy", "redirect"),
        )
        stdin_path = self._materialise_prompt(req, run_dir)
        inv = Invocation(
            argv=argv,
            env=env,
            cwd=self.adapter.cwd_for(req, ctx),
            stdin_devnull=stdin_path is None,
            stdin_file=stdin_path,
            encoding="utf-8",
        )

        state = TurnState(req=req, run_dir=run_dir, snapshot_since=snapshot_since)
        proc = self.platform.spawn(inv)
        reader: Optional[LineReader] = None
        stopped_early = False
        try:
            reader = self.platform.line_reader(proc)
            stopped_early = self._pump(reader, state, deadline, stop_event)
        finally:
            if reader is not None:
                reader.close()
            # 提前停下（deny / 超限 / 超时 / 取消）时必须先杀再等，否则会在
            # 一个故意睡很久的 agent 上白等满 wait 超时。
            if stopped_early:
                self.platform.terminate(proc, grace_s=1.0)
            try:
                state.exit_code = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 进程不退出就走强杀
                self.platform.terminate(proc, grace_s=2.0)
                try:
                    state.exit_code = proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    state.exit_code = None

        outcome = self._finish(req, state, before, workspace)
        self._write_run_record(run_dir, req, outcome, state)
        return outcome

    # ---- 扫描循环 -------------------------------------------------------
    def _pump(self, reader: LineReader, state: TurnState, deadline: float, stop_event) -> bool:
        """返回 True 表示"提前停下"（deny / 超限 / 取消），需要上层确保进程已终止。"""
        cfg = self.config
        while True:
            if stop_event is not None and stop_event.is_set():
                state.cancelled = True
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.timed_out = True
                return True
            kind, line = reader.next_line(min(cfg.read_slice_s, remaining))
            if kind == TIMEOUT:
                continue
            if kind == EOF:
                return False
            # 按**线上字节**计，而不是按字符：中文一个字 3 字节，按 len(str) 计数
            # 会把 8 MiB 的上限放大到 24 MiB。last_bytes 由读取器在切行时给出。
            state.output_bytes += int(getattr(reader, "last_bytes", 0) or 0)
            state.note_raw(line)
            limit = self.adapter.max_output_bytes or cfg.max_output_bytes
            if state.output_bytes > limit:
                state.stopped_early = True
                state.incomplete.append("output_budget")
                return True
            event = self.adapter.parse_line(line)
            if event is None:
                continue
            state.events.append(event)
            if event.kind == "tool":
                violation = self.adapter.deny_violation(state, event)
                if violation is not None:
                    state.denied_tool = event.tool_name or "unknown"
                    state.incomplete.append("denied_tool: " + violation)
                    return True
            self.adapter.on_event(state, event)

    # ---- 收尾 -----------------------------------------------------------
    def _finish(
        self,
        req: TurnRequest,
        state: TurnState,
        before,
        workspace: str,
    ) -> Outcome:
        if state.timed_out or state.cancelled:
            fallback = None
            if state.timed_out:
                fallback = self.adapter.finalize_on_timeout(state)
            if state.cancelled and not state.answer:
                outcome = self.adapter.make_outcome(
                    state, TurnStatus.CANCELLED, Reason.CANCELLED
                )
                if fallback:
                    outcome.answer = fallback
            else:
                outcome = self.adapter.classify(state)
                if fallback and not outcome.answer:
                    outcome.answer = fallback
                    outcome.fallbacks.append("finalize_on_timeout")
                    outcome.mark_incomplete("fallback_answer")
        else:
            outcome = self.adapter.classify(state)

        # 诚实字段：底座负责，adapter 无法"忘记"。
        # 不变式：**incomplete 必须带至少一条原因** —— 一个"没做完但不说为什么"的
        # 终态，和"做完了"一样会让下游无法处理。
        for reason in state.incomplete:
            outcome.mark_incomplete(reason)
        if outcome.status == TurnStatus.INCOMPLETE and not outcome.incomplete_reasons:
            outcome.incomplete_reasons.append(outcome.reason or "unspecified")
        if state.stopped_early and "output_budget" not in outcome.incomplete_reasons:
            outcome.mark_incomplete("output_budget")
        if req.model and not getattr(self.adapter, "accepts_model", False):
            outcome.ignored.append("model")
        if req.resume_key and not self.adapter.capabilities.supports_resume:
            # 今天三个真实 adapter 都不支持续跑：那就明说"你的 resume_key 没被用上"，
            # 而不是让调用方以为多轮上下文还在。
            outcome.ignored.append("resume_key")
        if req.narrow_to and not self.adapter.capabilities.denies_tools:
            outcome.ignored.append("narrow_to")
        outcome.turn_key = req.turn_key
        outcome.adapter = outcome.adapter or self.adapter.name
        outcome.exit_code = state.exit_code
        outcome.duration_s = round(time.monotonic() - state.started_at, 3) if state.started_at else 0.0
        if state.usage:
            outcome.usage = dict(state.usage)
        if state.resume_key:
            outcome.resume_key = state.resume_key

        if outcome.status == TurnStatus.FAILED and not outcome.error:
            # 真实运维需要"为什么失败"：CLI 的报错常常是不带 JSON 的裸文本
            # （实测 cursor-agent：`Error: EPERM ... mkdir ~/.cursor/projects/...` 被我们的
            # 解析器忽略，最后只剩一个 no_answer）。有 tail 就把它写进 error，别让排障的人猜。
            tail = " | ".join(state.raw_tail[-3:]).strip()
            if tail:
                outcome.error = tail[-600:]
        if outcome.answer:
            filtered = self._answer_filter(outcome.answer)
            if filtered != outcome.answer:
                outcome.fallbacks.append("secret_redacted")
                outcome.answer = filtered
        if before is not None:
            after = artifacts_mod.snapshot(
                workspace, max_files=self.config.snapshot_max_files,
                since=getattr(state, "snapshot_since", None)
            )
            outcome.artifacts = after.diff(before).produced
            # 诚实：撞上限或只看了窗口，都必须让人知道"这份产物清单可能不全"
            if after.truncated and "artifacts_truncated" not in outcome.fallbacks:
                outcome.fallbacks.append("artifacts_truncated")
            if after.windowed and "artifacts_windowed" not in outcome.fallbacks:
                outcome.fallbacks.append("artifacts_windowed")
        return outcome

    # ---- 小工具 ---------------------------------------------------------
    def _materialise_prompt(self, req: TurnRequest, run_dir: str) -> Optional[str]:
        text = self.adapter.prompt_on_stdin(req)
        if text is None:
            return None
        path = os.path.join(run_dir, "prompt.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # Windows 上 0600 不是权限，必须让平台用 ACL 收紧（prompt 里有用户消息）
        self.platform.harden_path(path, 0o600)
        return path

    def _validate_workspace(self, req: TurnRequest) -> str:
        value = (req.workspace or "").strip()
        if not value:
            raise ValueError("workspace is required")
        path = os.path.abspath(os.path.expanduser(value))
        if not os.path.isabs(path):
            raise ValueError("workspace must be an absolute path: %r" % (value,))
        if not os.path.isdir(path):
            raise ValueError("workspace is not a directory: %r" % (path,))
        return path

    def _write_run_record(
        self, run_dir: str, req: TurnRequest, outcome: Outcome, state: TurnState
    ) -> None:
        record: Dict[str, Any] = {
            "adapter": self.adapter.name,
            "capabilities": self.adapter.capabilities.to_dict(),
            "request": {
                "workspace": req.workspace,
                "timeout_s": req.timeout_s,
                "turn_key": req.turn_key,
                "model": req.model,
                "narrow_to": list(req.narrow_to),
            },
            "outcome": outcome.to_dict(),
            "events": len(state.events),
            "output_bytes": state.output_bytes,
            "tail": state.raw_tail[-20:],
        }
        path = os.path.join(run_dir, "run.json")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
            self.platform.harden_path(path, 0o600)
        except OSError:  # pragma: no cover - 记录失败不应把 turn 变成失败
            pass
