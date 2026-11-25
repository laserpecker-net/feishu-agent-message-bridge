"""把 agentkit 接到既有的 ``consumer_runtime.main()`` 上（**不改任何现有入口**）。

这一层的存在就是为了"切换成本接近零"：``consumer_runtime`` 要的只是一个
``build_agent(home) -> generate(payload, event, stop_event) -> str``。
既有三个入口各自实现了它（每个 700 行）；这里用 **20 行**实现同一个契约，
把差异全部收进 ``agentkit`` 的 adapter + platform。

于是迁移一个 AGENT 变成一次参数变更：

    # 旧：python3 dsh-consumer.py --home ~/.local/share/feishu-agent
    # 新：python3 agentkit-consumer.py --agent dsh --home ~/.local/share/feishu-agent

出错时的行为必须与既有入口一致，否则 bridge 侧的 fail 语义会漂：``generate``
**要么返回非空答案，要么抛异常**（``consumer_runtime`` 会转成
``fail --error AgentFailed``）。``incomplete`` 也按失败处理 —— 半截答案不许发出去。
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any, Dict, Mapping, Optional

from .adapters import get_adapter
from .adapters.base import AgentAdapter
from .session import SessionStore
from .turn import TurnConfig, TurnRunner
from .types import Outcome, TurnRequest, TurnStatus

#: bridge 的 ``kind`` 白名单（``bridge/store.py`` 只认这三个）
ADAPTER_KIND = {
    "dsh": "dsh",
    "codex": "codex",
    "cursor": "other",
    "fake": "other",
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class AgentRunFailed(RuntimeError):
    """``generate`` 的失败出口：带完整 Outcome，供运行时的日志/诊断使用。"""

    def __init__(self, outcome: Outcome):
        self.outcome = outcome
        detail = outcome.error or outcome.reason
        super(AgentRunFailed, self).__init__(
            "agent %s %s (%s)" % (outcome.adapter or "?", outcome.status, detail)
        )


def _build_prompt(payload: Mapping[str, Any], workspace: str, timeout_s: float,
                  owner_name: Optional[str] = None) -> str:
    from agent_prompt import build_prompt  # 复用既有提示词，避免两套行为模型漂移

    teachings = payload.get("teachings") if isinstance(payload, Mapping) else None
    return build_prompt(payload, workspace, timeout_seconds=timeout_s,
                        owner_name=owner_name, teachings=teachings)


def turn_key_for(event: Mapping[str, Any]) -> str:
    """幂等键 = 事件 + **本次 claim_token**。

    为什么带 token：同一个事件换一个 token 重新领取，是"上一次领取过期了"，
    这是**合法的第二次尝试**，不应该被重放；只有"同一个 token 再来一次"
    （进程崩溃后重放）才必须返回第一次的结果。
    """
    event_id = str(event.get("id") or "")
    token = str(event.get("claim_token") or "")
    digest = hashlib.sha256(("%s|%s" % (event_id, token)).encode("utf-8")).hexdigest()[:24]
    return "evt-%s-%s" % (event_id or "unknown", digest)


def session_key_for(adapter_name: str, payload: Mapping[str, Any]) -> str:
    chat = payload.get("chat_id") or "dm"
    return "%s:%s" % (adapter_name, chat)


def build_agent(
    adapter_name: str,
    home=None,
    workspace: Optional[str] = None,
    timeout_s: Optional[float] = None,
    platform=None,
    config: Optional[TurnConfig] = None,
    snapshot_window_s: float = 0.0,
):
    """返回 ``consumer_runtime.main()`` 需要的 ``build_agent(home)``。"""
    adapter: AgentAdapter = get_adapter(adapter_name)
    if config is None and snapshot_window_s:
        config = TurnConfig(snapshot_window_s=float(snapshot_window_s))
    default_workspace = workspace
    default_timeout = float(timeout_s or 240.0)

    def build(home) -> "callable":
        from consumer_runtime import owner_name_at  # 身份只有这一个来源（status.json.identity）

        root_home = os.path.abspath(os.path.expanduser(str(home)))
        ws = default_workspace or os.environ.get("FEISHU_CONSUMER_WORKSPACE") or root_home
        runner = TurnRunner(adapter, platform=platform, config=config)
        sessions = SessionStore(os.path.join(ws, ".agentkit", "sessions"))
        counter = {"turns": 0}

        def generate(payload: Mapping[str, Any], event: Mapping[str, Any], stop_event) -> str:
            if not isinstance(payload, Mapping):
                raise ValueError("Expected a message payload")
            prompt = _build_prompt(payload, ws, default_timeout, owner_name_at(root_home))
            session_key = session_key_for(adapter.name, payload)
            resume_key = None
            if adapter.capabilities.supports_resume:
                record = sessions.get(session_key)
                if record is not None:
                    resume_key = record.resume_key
            counter["turns"] += 1
            req = TurnRequest(
                prompt=prompt,
                workspace=ws,
                timeout_s=default_timeout,
                resume_key=resume_key,
                turn_key=turn_key_for(event),
                metadata={
                    "event_id": event.get("id"),
                    "claim_token": event.get("claim_token"),
                    "chat_id": payload.get("chat_id"),
                    "turn": counter["turns"],
                },
            )
            outcome = runner.run(req, stop_event=stop_event)
            # 会话指针：成功就记住新 key；被拒就当作"这个对话从零开始"
            if adapter.capabilities.supports_resume:
                if outcome.reason == "resume_rejected":
                    sessions.clear(session_key)
                elif outcome.status == TurnStatus.COMPLETED and outcome.resume_key:
                    sessions.set(session_key, outcome.resume_key, adapter=adapter.name)
            if outcome.status != TurnStatus.COMPLETED:
                raise AgentRunFailed(outcome)
            answer = (outcome.answer or "").strip()
            if not answer:
                outcome.status = TurnStatus.FAILED
                outcome.reason = "no_answer"
                raise AgentRunFailed(outcome)
            return answer

        generate.adapter = adapter  # type: ignore[attr-defined]
        generate.workspace = ws  # type: ignore[attr-defined]
        return generate

    build.adapter_name = adapter_name  # type: ignore[attr-defined]
    return build


def describe(adapter_name: str, home=None, workspace: Optional[str] = None) -> Dict[str, Any]:
    """给 ``--check`` 用：在不 register / 不 claim 的前提下把配置说清楚。"""
    adapter = get_adapter(adapter_name)
    ws = (
        workspace
        or os.environ.get("FEISHU_CONSUMER_WORKSPACE")
        or (os.path.abspath(os.path.expanduser(str(home))) if home else None)
    )
    info: Dict[str, Any] = {
        "adapter": adapter.name,
        "kind": ADAPTER_KIND.get(adapter.name, "other"),
        "capabilities": adapter.capabilities.to_dict(),
        "workspace": ws,
        "workspace_exists": bool(ws and os.path.isdir(ws)),
        "session_store": os.path.join(ws, ".agentkit", "sessions") if ws else None,
    }
    try:
        info["binary"] = adapter.resolve_binary(_platform_or_none())
        info["binary_ok"] = True
    except Exception as error:  # noqa: BLE001 - --check 的职责就是把问题说出来
        info["binary"] = None
        info["binary_ok"] = False
        info["binary_error"] = "%s: %s" % (type(error).__name__, error)
    return info


def _platform_or_none():
    from .platform import get_platform

    try:
        return get_platform()
    except Exception:  # noqa: BLE001 - --check 不该因为平台门禁而崩掉
        return None
