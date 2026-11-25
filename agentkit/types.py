"""agentkit 的值对象（不可变优先，便于幂等重放与测试断言）。

这些类型刻意做成"纯数据 + 可 JSON 序列化"，因为 turn 的终态要被落盘重放
（见 ``idempotency.py``）：一个不能被序列化的结果对象，等于没有幂等。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


class TurnStatus(object):
    """一次 turn 的终态。取值与 UHP 的 response status 对齐。

    ``INCOMPLETE`` 的存在是刻意的：预算把工作截断时必须报它，**禁止**报
    ``COMPLETED``（规范原文："Never report completed if the budget cut the work short"）。
    一个"跑完了但没做完"的 turn 如果被标成完成，下游所有错误处理都失去意义。
    """

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = (COMPLETED, INCOMPLETE, FAILED, CANCELLED)


class Reason(object):
    """``Outcome.reason`` 的常用取值（adapter 可以给更细的字符串）。"""

    OK = "ok"
    # 时间预算耗尽，答案来自 fallback 通道
    TIME_BUDGET = "time_budget"
    # 步数/工具预算耗尽
    TOOL_BUDGET = "tool_budget"
    # 被 deny 策略拦下（adapter 的防空转兜底）
    DENIED_TOOL = "denied_tool"
    # 进程非零退出且没有可用答案
    NONZERO_EXIT = "nonzero_exit"
    # 模型/CLI 明确报错
    AGENT_ERROR = "agent_error"
    # 起不来（可执行文件不存在、权限、平台不支持）
    SPAWN_FAILED = "spawn_failed"
    # 外部取消（信号、父进程要求）
    CANCELLED = "cancelled"
    # 跑完了但没解析出答案
    NO_ANSWER = "no_answer"
    # 续跑被拒（会话不存在/已过期）：允许底座降级成新会话再试一次
    RESUME_REJECTED = "resume_rejected"


@dataclass(frozen=True)
class Capabilities:
    """一个 adapter 的静态能力声明。

    调用方必须先读再假设（与 UHP 的 ``capabilities`` 同一原则）：服务器/适配器必须为
    不支持的能力显式报 False，**不允许省略**。省略会让"没实现"和"实现了但没说"无法区分。
    """

    name: str
    stream_format: str = "unknown"
    supports_resume: bool = False
    supports_model_switch: bool = False
    emits_tool_events: bool = False
    emits_usage: bool = False
    denies_tools: bool = False
    deny_enforcement: str = "none"  # 'argv' | 'stream-backstop' | 'policy-file' | 'none'
    needs_pseudo_tty: bool = False
    supports_artifacts: bool = True
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TurnRequest:
    """调用方视角的一次 turn。与具体 AGENT 无关。"""

    prompt: str
    workspace: str
    timeout_s: float = 240.0
    # 续跑用的键（agent 自己的 session id / thread id），由上一次 Outcome 带回
    resume_key: Optional[str] = None
    model: Optional[str] = None
    # 调用方追加的环境变量（会在平台层做白名单过滤，不允许覆盖受保护变量）
    extra_env: Mapping[str, str] = field(default_factory=dict)
    # 幂等键：同一个 key 的重复提交必须返回第一次的结果，而不是再跑一遍
    turn_key: Optional[str] = None
    # 允许调用的工具子集等"收窄"约束；只允许收窄，不允许放宽
    narrow_to: Tuple[str, ...] = ()
    #: 续跑被拒时是否允许底座换个新会话重试一次（默认允许；关掉可拿到原始失败）
    allow_resume_fallback: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def narrowed(self, allowed: Sequence[str]) -> "TurnRequest":
        """返回一个只收窄、不放宽的副本（"narrowing is safe, widening is escalation"）。"""
        allowed_set = set(allowed)
        if self.narrow_to:
            allowed_set &= set(self.narrow_to)
        return TurnRequest(
            prompt=self.prompt,
            workspace=self.workspace,
            timeout_s=self.timeout_s,
            resume_key=self.resume_key,
            model=self.model,
            extra_env=self.extra_env,
            turn_key=self.turn_key,
            narrow_to=tuple(sorted(allowed_set)),
            metadata=self.metadata,
        )


@dataclass(frozen=True)
class Invocation:
    """adapter 交给平台层去 execute 的东西：一个完整、可执行的调用描述。

    平台层是唯一创建子进程的地方，所以 ``stdin_devnull`` 这类卫生项由平台强制，
    adapter 无法忘记（这正是把卫生项放平台层而不是各 adapter 里的原因）。
    """

    argv: Sequence[str]
    env: Mapping[str, str]
    cwd: str
    stdin_devnull: bool = True
    # 需要从 stdin 喂 prompt 的 CLI（codex / dsh 就是这种）走这里；
    # 由平台层写成 workspace 内的私有临时文件再重定向，而不是用 PIPE ——
    # PIPE 在 prompt 较大时会死锁（父进程写满缓冲区，子进程还没开始读）。
    stdin_text: Optional[str] = None
    # 已落盘的 prompt 文件路径（底座写入 0600 私有文件后交平台重定向为 stdin）
    stdin_file: Optional[str] = None
    # 期望的输出编码（Windows 控制台常是 cp936，必须显式钉住 UTF-8）
    encoding: str = "utf-8"


@dataclass(frozen=True)
class StreamEvent:
    """从 AGENT 的流里解析出来的一条事件（归一化后的最小形状）。"""

    kind: str  # 'init' | 'tool' | 'text' | 'status' | 'note' | 'usage' | 'result' | 'error' | 'other'
    raw: Mapping[str, Any] = field(default_factory=dict)
    tool_name: Optional[str] = None
    tool_command: Optional[str] = None
    text: Optional[str] = None
    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DenyPolicy:
    """一次 turn 内禁止的"能力"描述。

    adapter 各自填充（cursor 用工具名 + 命令动词，codex 用 item type…），
    ``TurnRunner`` 只在流上执行它 —— 因为**不能相信 CLI 自己会执行**：
    HarnessRouter 在 Claude 上实测到 ``--disallowedTools`` 被
    ``--dangerously-skip-permissions`` 静默变成空操作，而产品文档宣称"硬阻止"。
    结论是：deny 必须在"我们自己的进程"里再判一次。
    """

    tool_names: Tuple[str, ...] = ()
    tool_prefixes: Tuple[str, ...] = ()
    shell_verbs: Tuple[str, ...] = ()
    path_globs: Tuple[str, ...] = ()
    description: str = ""

    def violation(self, ev: StreamEvent) -> Optional[str]:
        """返回违规说明；不违规返回 None。"""
        if ev.kind != "tool":
            return None
        name = (ev.tool_name or "").strip().lower()
        # 归一化：`Delete(**)` / `tool_delete` / `deleteToolCall` 都收成 `delete`
        for cut in ("(", "toolcall"):
            idx = name.find(cut)
            if idx > 0:
                name = name[:idx]
        if name.startswith("tool_"):
            name = name[len("tool_"):]
        if name and name in self.tool_names:
            return "tool %r is on the deny list" % (ev.tool_name,)
        for prefix in self.tool_prefixes:
            if name.startswith(prefix):
                return "tool %r matches denied prefix %r" % (ev.tool_name, prefix)
        command = (ev.tool_command or "").lower()
        if command:
            for verb in self.shell_verbs:
                if verb and verb in command:
                    return "command %r contains denied verb %r" % (ev.tool_command, verb)
        return None


@dataclass
class Outcome:
    """一次 turn 的结果。``to_dict`` 之后可以原样 JSON 落盘并被幂等重放。"""

    status: str
    answer: Optional[str] = None
    reason: str = Reason.OK
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    # 任何"没做到"的地方都必须在这里说清楚，而不是只写日志
    incomplete_reasons: List[str] = field(default_factory=list)
    # 被忽略/被替换的东西（对应 UHP 的 ignored_fields / model_fallback）
    ignored: List[str] = field(default_factory=list)
    fallbacks: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    resume_key: Optional[str] = None
    served_model: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    denied_tool: Optional[str] = None
    error: Optional[str] = None
    turn_key: Optional[str] = None
    replayed: bool = False
    #: 实际起了几次进程（>1 说明发生过降级重试，例如续跑被拒）
    attempts: int = 1
    adapter: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TurnStatus.TERMINAL

    def mark_incomplete(self, why: str) -> "Outcome":
        if why and why not in self.incomplete_reasons:
            self.incomplete_reasons.append(why)
        if self.status == TurnStatus.COMPLETED:
            self.status = TurnStatus.INCOMPLETE
        return self

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Outcome":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in dict(data).items() if k in known})  # type: ignore[arg-type]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
