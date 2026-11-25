"""一个"假 AGENT"：给契约测试与新手当模板用。

它不调用任何真实 CLI，而是用当前解释器跑一段脚本并输出 JSON 行，因此：

* 跨平台（只依赖 ``sys.executable``）；
* 零网络、零凭据、毫秒级；
* 覆盖了 adapter 必须处理的全部形状：文本、工具调用、终态、非零退出、超时、
  以及"续跑被拒"（会话不存在/已过期）。

新增一个真实 AGENT 时，照这个类的六个钩子填空即可。
"""

from __future__ import annotations

import json
import sys
from typing import Dict, Mapping, Optional, Sequence

from ..types import (
    Capabilities,
    DenyPolicy,
    Outcome,
    Reason,
    StreamEvent,
    TurnRequest,
    TurnStatus,
)
from .base import AgentAdapter, PrepareContext, TurnState
from .registry import register

#: 假 AGENT 的脚本：行为全从环境变量读，输出与真实 CLI 相似的多行 JSON。
_SCRIPT = r"""
import json, os, sys, time
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
behaviour = json.loads(os.environ.get("FAKE_AGENT_BEHAVIOUR", "{}"))
def emit(**kw):
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()
resume = os.environ.get("FAKE_AGENT_SESSION") or None
if resume and behaviour.get("reject_resume"):
    emit(type="error", message="session not found: " + resume)
    sys.exit(1)
session = behaviour.get("new_session") or resume or "fake-session-1"
emit(type="init", session_id=session)
if behaviour.get("sleep_s"):
    time.sleep(float(behaviour["sleep_s"]))
for name in behaviour.get("tools", []):
    emit(type="tool_call", name=name, command=behaviour.get("command"), input={"path": "/tmp/x"})
for chunk in behaviour.get("text", []):
    emit(type="text", text=chunk)
if behaviour.get("mark_file"):
    with open(os.path.join(os.getcwd(), behaviour["mark_file"]), "a", encoding="utf-8") as handle:
        handle.write("run\n")
if behaviour.get("create_file"):
    path = os.path.join(os.getcwd(), behaviour["create_file"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("produced by fake agent\n")
if not behaviour.get("no_final"):
    emit(type="final", text=behaviour.get("answer", ""))
if behaviour.get("exit_code"):
    sys.exit(int(behaviour["exit_code"]))
"""


@register
class FakeAdapter(AgentAdapter):
    name = "fake"
    aliases = ("echo",)
    binary_name = ""
    binary_env = ""
    capabilities = Capabilities(
        name="fake",
        stream_format="jsonl",
        supports_resume=True,
        supports_model_switch=True,
        emits_tool_events=True,
        emits_usage=True,
        denies_tools=True,
        deny_enforcement="stream-backstop",
        notes="测试/模板用；不调用真实 CLI；可模拟续跑被拒",
    )
    #: 假 AGENT 会读 ``--model``，所以请求里的 model 不算被忽略
    accepts_model = True

    def resolve_binary(self, platform) -> str:
        # 假 AGENT 的"二进制"就是当前解释器：跨平台、无需安装
        return sys.executable

    def build_argv(self, req: TurnRequest, ctx: PrepareContext) -> Sequence[str]:
        argv = [ctx.binary, "-c", _SCRIPT]
        if req.model:
            argv += ["--model", str(req.model)]
        return argv

    def prompt_on_stdin(self, req: TurnRequest) -> Optional[str]:
        return req.prompt

    def env_patch(self, req: TurnRequest, ctx: PrepareContext) -> Mapping[str, str]:
        behaviour = dict(req.metadata.get("behaviour") or {})
        patch: Dict[str, str] = {}
        # 显式给了行为才覆盖；否则让父进程的 FAKE_AGENT_BEHAVIOUR 透传下去
        # （端到端测试走 driver，不经过 TurnRequest.metadata）
        if behaviour:
            patch["FAKE_AGENT_BEHAVIOUR"] = json.dumps(behaviour)
        if req.resume_key:
            patch["FAKE_AGENT_SESSION"] = req.resume_key
        return patch

    def deny_policy(self, req: TurnRequest) -> DenyPolicy:
        return DenyPolicy(
            tool_names=("delete", "remove", "subagent", "mcp"),
            tool_prefixes=("mcp", "subagent", "spawn", "collab"),
            shell_verbs=("rm -rf", "mkfs", "diskpart"),
            description="fake agent: destructive / delegation / mcp are denied",
        )

    def parse_line(self, line: str) -> Optional[StreamEvent]:
        text = line.strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, Mapping):
            return None
        kind = str(payload.get("type") or "")
        if kind == "tool_call":
            return StreamEvent(
                kind="tool",
                raw=payload,
                tool_name=str(payload.get("name") or ""),
                tool_command=str(payload.get("command") or "") or None,
            )
        if kind == "text":
            return StreamEvent(kind="text", raw=payload, text=str(payload.get("text") or ""))
        if kind == "final":
            return StreamEvent(kind="result", raw=payload)
        if kind == "init":
            return StreamEvent(kind="init", raw=payload)
        if kind == "error":
            return StreamEvent(kind="error", raw=payload)
        return StreamEvent(kind="other", raw=payload)

    def on_event(self, state: TurnState, event: StreamEvent) -> None:
        if event.kind == "init":
            session = (event.raw or {}).get("session_id")
            if isinstance(session, str) and session:
                state.resume_key = session
        elif event.kind == "text" and event.text:
            state.add_text(event.text)
        elif event.kind == "error":
            message = str((event.raw or {}).get("message") or "")
            # 真实 CLI 在会话失效时的措辞五花八门，这里统一认"错误里带 session"
            if state.req.resume_key and "session" in message.lower():
                state.extra["resume_rejected"] = True
                state.extra["resume_error"] = message
        elif event.kind == "result":
            final = (event.raw or {}).get("text")
            if isinstance(final, str) and final != "":
                state.answer = final
            state.usage["output_tokens"] = len(state.text)

    def classify(self, state: TurnState) -> Outcome:
        # 续跑被拒：能不能降级成新会话重试，由底座的策略决定（见 TurnRunner.run）
        if state.extra.get("resume_rejected"):
            return self.make_outcome(state, TurnStatus.FAILED, Reason.RESUME_REJECTED)
        if state.denied_tool:
            return self.make_outcome(state, TurnStatus.FAILED, Reason.DENIED_TOOL)
        if state.timed_out:
            # 时间到但有部分文本：报 incomplete 而不是 completed
            answer = state.answer or (state.text.strip() or None)
            return self.make_outcome(state, TurnStatus.INCOMPLETE, Reason.TIME_BUDGET, answer)
        if state.answer:
            return self.make_outcome(state, TurnStatus.COMPLETED, Reason.OK, state.answer)
        if state.exit_code not in (0, None):
            return self.make_outcome(state, TurnStatus.FAILED, Reason.NONZERO_EXIT)
        if state.text.strip():
            # 跑完了但没有 final 事件：不许当成功
            return self.make_outcome(state, TurnStatus.INCOMPLETE, Reason.NO_ANSWER,
                                     state.text.strip())
        return self.make_outcome(state, TurnStatus.FAILED, Reason.NO_ANSWER)
