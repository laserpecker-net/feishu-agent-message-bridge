"""``AgentAdapter``：Adapter 模式的抽象产品。

**能抽走的（已在平台层/模板层）**：起进程、组环境、读流、超时、杀进程树、产物比对、幂等。
**抽不走的（必须留在这里）**：argv 与 flag、流的事件 schema、最终答案的提取与校验、
静态权限载体怎么写、退出码语义。

判断标准很简单：**"换一个 AGENT 会不会变"** —— 会变的就是适配器的事，
不会变的就是底座的事（换 AGENT 不变 = 平台/模板层）。

一个 adapter 只允许做"收窄"：可以关掉工具、可以把 prompt 压到 stdin、可以把工作区
设成只读；**不允许**在单次请求里放宽静态策略（"narrowing is safe, widening is escalation"）。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from ..types import (
    Capabilities,
    DenyPolicy,
    Invocation,
    Outcome,
    Reason,
    StreamEvent,
    TurnRequest,
    TurnStatus,
)

#: 诊断用：保留最近多少条原始行（出错时要能回答"它到底吐了什么"）
RAW_TAIL_LINES = 40


@dataclass
class TurnState:
    """一次 turn 的累积状态。由 ``TurnRunner`` 创建，adapter 只读并追加。

    刻意做成可变对象：流式解析需要跨行累积（工具调用的名字可能先到、参数后到）。
    """

    req: TurnRequest
    run_dir: str
    started_at: float = field(default_factory=time.monotonic)
    events: List[StreamEvent] = field(default_factory=list)
    text_parts: List[str] = field(default_factory=list)
    answer: Optional[str] = None
    resume_key: Optional[str] = None
    served_model: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    violations: List[str] = field(default_factory=list)
    incomplete: List[str] = field(default_factory=list)
    denied_tool: Optional[str] = None
    exit_code: Optional[int] = None
    output_bytes: int = 0
    timed_out: bool = False
    cancelled: bool = False
    stopped_early: bool = False
    raw_tail: List[str] = field(default_factory=list)
    #: 产物快照的窗口起点（None = 全量快照）。由 turn 层写入、_finish 读取。
    snapshot_since: Optional[float] = None
    final_text: Optional[str] = None
    extra: Dict[str, object] = field(default_factory=dict)

    # ---- 便捷方法（adapter 用）------------------------------------------
    def add_text(self, text: str) -> None:
        if text:
            self.text_parts.append(text)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def add_usage(self, key: str, value: int) -> None:
        """**替换而非累加**：重试会重报同一个 step，累加就是重复计费。

        这条来自 HarnessRouter 对 DSH 的实测修复（``_dsh_to_claude`` 的 docstring）：
        usage 必须按 ``(turn, step)`` 键存放并覆盖。
        """
        del key, value  # 子类/adapter 在自己的键空间里维护，避免误用累加语义

    def note_raw(self, line: str) -> None:
        self.raw_tail.append(line[:2000])
        if len(self.raw_tail) > RAW_TAIL_LINES:
            del self.raw_tail[: len(self.raw_tail) - RAW_TAIL_LINES]


@dataclass(frozen=True)
class PrepareContext:
    """adapter 组装 argv 时能看到的、由底座提供的确定性事实。"""

    workspace: str
    run_dir: str
    agentkit_home: str
    agentkit_tmp: str
    binary: str
    deadline: float
    #: 平台实例（adapter 需要收紧文件权限/查进程时用；不要用它起进程）
    platform: object = None


class AgentAdapter(ABC):
    """一个 AGENT CLI 的适配器。子类至少要给出名字、能力声明和四个钩子。"""

    #: 注册名（小写、无空格）
    name: str = ""
    #: 别名（例如 ``'claude-code'`` → ``'claude'``）
    aliases: Sequence[str] = ()
    #: 二进制名（用于 ``shutil.which`` 探测）
    binary_name: str = ""
    #: 二进制路径的环境变量覆盖名（与现有代码一致：``CODEX_CONSUMER_BINARY`` 之类）
    binary_env: str = ""
    #: 能力声明必须显式、不许省略（UHP 的 capabilities 原则）
    capabilities: Capabilities = Capabilities(name="abstract")
    #: HOME 策略：``redirect``（默认，收进 workspace）或 ``inherit``（保留真实 $HOME）。
    #: 只有"登录态就放在 $HOME 下、且没有可复制的凭据文件"的 AGENT 才允许 inherit，
    #: 并且必须在 capabilities.notes 写明理由 —— 这是**窄化例外**，不是通用开关。
    home_policy: str = "redirect"
    #: 本 AGENT 自己的输出上限（0 = 用底座配置）。cursor 的 MAX_OUTPUT 是 8 MiB，
    #: 与 legacy 保持同一个数，避免"换个入口上限就变了"。
    max_output_bytes: int = 0

    # ---- 钩子：组装调用 ------------------------------------------------
    @abstractmethod
    def build_argv(self, req: TurnRequest, ctx: PrepareContext) -> Sequence[str]:
        """构造 argv。agent 特有的 flag 全部在这里，底座不干预。"""

    def cwd_for(self, req: TurnRequest, ctx: "PrepareContext") -> str:
        """子进程的 cwd。默认就是工作区。

        为什么允许覆盖：Cursor 的 legacy 实现刻意让 cwd 是一个**私有空目录**
        （策略文件写在那里，`bridge/agent.py:503-512`），工作区只通过 `--add-dir` 暴露。
        默认改成 workspace 就等于悄悄放宽"相对路径能写到哪"。
        """
        return ctx.workspace

    def prompt_on_stdin(self, req: TurnRequest) -> Optional[str]:
        """prompt 走 stdin 的 CLI 覆盖它（codex / dsh 就是这种）。

        底座会把它写成 workspace 内的私有临时文件再重定向 —— 不用 PIPE，因为
        prompt 大于管道缓冲区时会死锁（父进程写满、子进程还没开始读）。
        """
        return None

    def env_patch(self, req: TurnRequest, ctx: PrepareContext) -> Mapping[str, str]:
        """adapter 需要的额外环境变量（例如 ``DSH_PERMISSION_MODE``）。

        受保护变量（HOME/PWD/TMPDIR/PATH…）由平台层拥有，这里改会被拒。
        """
        return {}

    def allow_secret_env(self, req: TurnRequest) -> Sequence[str]:
        """允许进入子进程的、名字像密钥的变量名（默认一个都不放）。

        默认清洗是刻意的：真凭据不该出现在 agent 进程的环境里。需要凭据的 CLI 应该
        走"占位符 + 本地 relay"或它自己的登录态文件，而不是把 key 塞进 env。
        """
        return ()

    def preflight(self, req: TurnRequest, ctx: PrepareContext) -> None:
        """起进程之前的检查（二进制存在、登录态文件可读…）。失败抛异常即 fail-closed。"""

    # ---- 钩子：解析与判定 ----------------------------------------------
    def deny_policy(self, req: TurnRequest) -> DenyPolicy:
        """静态 deny 描述。底座会在**流上**执行它（不信 CLI 自己会执行）。"""
        return DenyPolicy()

    @abstractmethod
    def parse_line(self, line: str) -> Optional[StreamEvent]:
        """把一行输出解析成归一化事件；不认识的返回 None（**不要抛异常**）。"""

    def deny_violation(self, state: TurnState, event: StreamEvent) -> Optional[str]:
        """流上的 deny 判定。默认用静态 :class:`DenyPolicy`。

        需要精确复用既有策略的 adapter（本仓的 cursor/codex/dsh 都复用
        ``consumer_safety.tool_violation``）覆盖这个方法即可 —— 于是"策略只有一份"
        与"底座负责执行"同时成立。
        """
        return self.deny_policy(state.req).violation(event)

    def on_event(self, state: TurnState, event: StreamEvent) -> None:
        """事件到达时的累积逻辑（记录终态、抽 session id、累计 usage…）。"""

    @abstractmethod
    def classify(self, state: TurnState) -> Outcome:
        """根据累积状态与退出码给出 Outcome（status/answer/reason）。"""

    def finalize_on_timeout(self, state: TurnState) -> Optional[str]:
        """时间到但还没答时，能否用已收集的内容再拼一个答案。

        默认不能。能做的 adapter（例如 Cursor 的 finalize 二次 pass）返回文本，
        底座会把它标成 ``incomplete`` —— **绝不允许**把截断的结果标成 completed。
        """
        return None

    # ---- 通用工具 ------------------------------------------------------
    def resolve_binary(self, platform) -> str:
        """二进制解析：环境变量覆盖优先，其次 PATH 查找，最后报错（fail-closed）。"""
        import os

        candidate = ""
        if self.binary_env:
            candidate = os.environ.get(self.binary_env, "").strip()
        if not candidate and self.binary_name:
            candidate = platform.which(self.binary_name) or ""
        if not candidate:
            raise FileNotFoundError(
                "agent %r: no executable found for %r (set %s)"
                % (self.name, self.binary_name, self.binary_env or "<no override>")
            )
        return candidate

    def make_outcome(self, state: TurnState, status: str, reason: str,
                     answer: Optional[str] = None) -> Outcome:
        return Outcome(
            status=status,
            answer=answer,
            reason=reason,
            exit_code=state.exit_code,
            duration_s=round(time.monotonic() - state.started_at, 3),
            resume_key=state.resume_key,
            served_model=state.served_model,
            usage=dict(state.usage),
            denied_tool=state.denied_tool,
            adapter=self.name,
        )
