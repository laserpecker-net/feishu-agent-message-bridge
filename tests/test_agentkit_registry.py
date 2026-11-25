"""注册表 / 工厂 / 与既有策略的一致性。

最后一组最重要：**新的适配层不允许改变既有安全策略**。所以这里直接拿
``consumer_safety`` 的真函数当对照物，断言同一输入得到同一结论。
"""

import pytest

from agentkit import errors
from agentkit.adapters import (
    ADAPTER_ENV,
    adapter_class,
    get_adapter,
    is_registered,
    list_adapters,
    registry_snapshot,
)
from agentkit.adapters.registry import register, reset_for_tests
from agentkit.types import DenyPolicy, StreamEvent


def test_builtin_adapters_are_registered():
    names = list_adapters()
    for expected in ("fake", "cursor", "codex", "dsh"):
        assert expected in names, names
    snapshot = registry_snapshot()
    assert snapshot["deepseek-harness"]  # 别名也在表里


def test_alias_resolves_to_the_same_class():
    assert adapter_class("dsh") is adapter_class("deepseek-harness")
    assert get_adapter("deepseek-harness").name == "dsh"


def test_env_var_selects_the_adapter(monkeypatch):
    monkeypatch.setenv(ADAPTER_ENV, "codex")
    assert get_adapter().name == "codex"


def test_unknown_adapter_fails_closed_with_the_known_list():
    with pytest.raises(errors.AdapterNotFound) as excinfo:
        adapter_class("no-such-agent")
    message = str(excinfo.value)
    assert "no-such-agent" in message
    assert "fake" in message  # 报错要把可选项列出来，否则排查靠猜


def test_duplicate_registration_is_refused():
    from agentkit.adapters.base import AgentAdapter
    from agentkit.types import Capabilities

    class Dup(AgentAdapter):
        name = "codex"  # 与内置冲突
        capabilities = Capabilities(name="dup")

        def build_argv(self, req, ctx):
            return []

        def parse_line(self, line):
            return None

        def classify(self, state):
            raise NotImplementedError

    with pytest.raises(ValueError):
        register(Dup)


def test_every_adapter_declares_the_full_capability_set():
    """能力必须"显式声明"，不许靠省略表达"不支持"。"""
    required = {
        "name",
        "stream_format",
        "supports_resume",
        "supports_model_switch",
        "emits_tool_events",
        "emits_usage",
        "denies_tools",
        "deny_enforcement",
        "supports_artifacts",
    }
    for name in list_adapters():
        caps = get_adapter(name).capabilities.to_dict()
        assert required <= set(caps), (name, required - set(caps))


# ---------------------------------------------------------------------------
# 与既有策略的一致性（这是"没偷偷改安全"的证据）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("delete", {"path": "/tmp/x"}),
        ("mcp__github__create_issue", {"title": "x"}),
        ("subagent", {"prompt": "x"}),
        ("read", {"path": "/tmp/x"}),
        ("shell", {"command": "rm -rf /"}),
        ("shell", {"command": "ls -la"}),
    ],
)
def test_cursor_deny_matches_the_shared_policy(tool, arguments):
    import consumer_safety

    adapter = get_adapter("cursor")
    state = type("S", (), {"req": None})()
    event = StreamEvent(kind="tool", raw={"arguments": arguments}, tool_name=tool,
                        tool_command=(arguments or {}).get("command"))
    decision = adapter.deny_violation(state, event)
    expected = consumer_safety.tool_violation(tool, arguments)
    if expected is not None:
        assert decision is not None
    else:
        # 共享策略放过的，adapter 也不许自己加码（否则就是静默改变行为）
        assert decision is None


def test_codex_argv_comes_from_the_existing_command_builder():
    """flag 集合只有一处真相：直接复用 ``codex-consumer._command``。"""
    import pathlib

    from agentkit.adapters.legacy import CodexAdapter, _load

    module = _load("codex", "codex-consumer.py")
    adapter = CodexAdapter()
    ctx = type(
        "C",
        (),
        {"binary": "/usr/local/bin/codex", "run_dir": "/tmp/run", "workspace": "/tmp/ws"},
    )()
    argv = list(adapter.build_argv(None, ctx))
    expected = list(module._command("/usr/local/bin/codex", pathlib.Path("/tmp/run")))
    assert argv == expected
    assert "--sandbox" in argv and "--json" in argv
    assert argv[-1] == "-"  # prompt 走 stdin


def test_dsh_argv_and_overlay_come_from_the_existing_module(tmp_path):
    from agentkit.adapters.legacy import DshAdapter, _load

    module = _load("dsh", "dsh-consumer.py")
    adapter = DshAdapter()
    ctx = type(
        "C",
        (),
        {
            "binary": "/usr/local/bin/dsh",
            "run_dir": str(tmp_path),
            "workspace": str(tmp_path),
        },
    )()
    req = type("R", (), {"workspace": str(tmp_path)})()
    adapter.preflight(req, ctx)
    overlay = tmp_path / module.OVERLAY_NAME
    assert overlay.is_file()
    argv = list(adapter.build_argv(req, ctx))
    assert argv[:2] == ["/usr/local/bin/dsh", "headless"]
    assert "--patch" in argv and "--json" in argv and argv[-1] == "-"


def test_cursor_prompt_goes_on_argv_not_stdin():
    from agentkit.adapters.legacy import CursorAdapter

    adapter = CursorAdapter()
    ctx = type("C", (), {"binary": "/usr/local/bin/cursor", "run_dir": "/tmp/r",
                         "workspace": "/tmp/ws"})()
    req = type("R", (), {"prompt": "hello", "workspace": "/tmp/ws"})()
    argv = list(adapter.build_argv(req, ctx))
    assert argv[-1] == "hello"
    assert adapter.prompt_on_stdin(req) is None


# ---------------------------------------------------------------------------
# 解析器：用真实形状的载荷验证归一化
# ---------------------------------------------------------------------------
def test_cursor_parses_tool_and_result_lines():
    adapter = get_adapter("cursor")
    tool = adapter.parse_line('{"type":"tool_call","name":"Bash","command":"ls"}')
    assert tool is not None and tool.kind == "tool" and tool.tool_name == "Bash"
    result = adapter.parse_line('{"type":"result","subtype":"success","result":"ok"}')
    assert result is not None and result.kind == "result"
    assert adapter.parse_line("not json") is None
    assert adapter.parse_line("") is None


def test_codex_denies_unknown_item_types():
    adapter = get_adapter("codex")
    event = adapter.parse_line('{"type":"item.completed","item":{"type":"mcp_tool_call"}}')
    assert event is not None and event.kind == "tool"
    state = type("S", (), {"req": None})()
    assert adapter.deny_violation(state, event) is not None


def test_dsh_fails_closed_on_events_outside_the_allowed_set():
    adapter = get_adapter("dsh")
    assert adapter.parse_line('{"type":"final","text":"done"}').kind == "result"
    # legacy: 'Unexpected DSH event' —— 未知事件必须变成错误，不能静默忽略
    unknown = adapter.parse_line('{"type":"something_new"}')
    assert unknown is not None and unknown.kind == "error"


def test_deny_policy_normalizes_tool_names():
    policy = DenyPolicy(tool_names=("delete",), tool_prefixes=("mcp",))
    for name in ("Delete(**) ", "tool_delete", "deleteToolCall"):
        ev = StreamEvent(kind="tool", tool_name=name)
        assert policy.violation(ev) is not None, name
    assert policy.violation(StreamEvent(kind="tool", tool_name="read")) is None
