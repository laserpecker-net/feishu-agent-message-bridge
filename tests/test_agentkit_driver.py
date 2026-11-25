"""driver + SessionStore：把 agentkit 接到 ``consumer_runtime`` 的那一层。

用假 AGENT 走**真实**的 ``generate(payload, event, stop_event)`` 契约，因此这里验证的是
既有运行时真正依赖的东西：要么返回非空答案，要么抛异常（运行时才会 fail 掉这个事件）。
"""

import json
import os
import threading

import pytest

from agentkit import driver
from agentkit.session import SessionStore
from agentkit.turn import TurnConfig

PAYLOAD = {
    "message_id": "om_1",
    "chat_id": "oc_chat_1",
    "sender_type": "user",
    "text": "帮我看看今天的日程",
}


def event(event_id="7", token="tok-a"):
    return {"id": event_id, "claim_token": token, "kind": "message"}


def build(tmp_path, name="fake", **kwargs):
    """造一个真正的 ``generate(payload, event, stop_event)``（与运行时拿到的完全一致）。"""
    factory = driver.build_agent(name, workspace=str(tmp_path), **kwargs)
    return factory(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env():
    saved = os.environ.get("FAKE_AGENT_BEHAVIOUR")
    yield
    if saved is None:
        os.environ.pop("FAKE_AGENT_BEHAVIOUR", None)
    else:
        os.environ["FAKE_AGENT_BEHAVIOUR"] = saved


def test_generate_returns_the_answer(tmp_path):
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": "今天两个会"})
    generate = build(tmp_path)
    assert generate(PAYLOAD, event(), threading.Event()) == "今天两个会"


def test_prompt_uses_the_shared_builder(tmp_path):
    """提示词必须来自 ``agent_prompt.build_prompt``，不能在 driver 里另起一套。"""
    import agent_prompt

    seen = {}

    def fake_agent(payload, workspace=None, timeout_seconds=0, owner_name=None):
        seen["payload"] = payload
        seen["workspace"] = workspace
        seen["timeout"] = timeout_seconds
        seen["owner_name"] = owner_name
        return "来自共享提示词"

    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": "ok"})
    import agentkit.driver as driver_mod

    original = driver_mod._build_prompt
    driver_mod._build_prompt = lambda payload, workspace, timeout_s, owner_name=None: fake_agent(
        payload, workspace, timeout_s, owner_name
    )
    try:
        generate = build(tmp_path)
        assert generate(PAYLOAD, event(), threading.Event()) == "ok"
    finally:
        driver_mod._build_prompt = original
    assert seen["payload"] is PAYLOAD
    assert seen["workspace"] == str(tmp_path)
    assert seen["timeout"] == 240.0
    assert callable(agent_prompt.build_prompt)


def test_failure_raises_so_the_runtime_can_fail_the_event(tmp_path):
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"text": ["只有半截"], "no_final": True})
    generate = build(tmp_path)
    with pytest.raises(driver.AgentRunFailed) as excinfo:
        generate(PAYLOAD, event(), threading.Event())
    outcome = excinfo.value.outcome
    # incomplete 也必须当成失败：半截答案不许发到飞书
    assert outcome.status in ("incomplete", "failed")


def test_empty_answer_is_never_returned(tmp_path):
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": ""})
    generate = build(tmp_path)
    with pytest.raises(driver.AgentRunFailed):
        generate(PAYLOAD, event(), threading.Event())


def test_same_event_and_token_runs_only_once(tmp_path):
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": "只该跑一次", "mark_file": "ran.log"})
    generate = build(tmp_path)
    first = generate(PAYLOAD, event(), threading.Event())
    second = generate(PAYLOAD, event(), threading.Event())  # 同一 event + 同一 token
    assert first == second == "只该跑一次"
    marks = (tmp_path / "ran.log").read_text(encoding="utf-8").splitlines()
    assert len(marks) == 1, marks  # 真的只起了一次进程


def test_new_claim_token_runs_again(tmp_path):
    """换 token = 上一次领取过期了，这是合法的第二次尝试，不该被重放。"""
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": "ok", "mark_file": "ran.log"})
    generate = build(tmp_path)
    generate(PAYLOAD, event(token="tok-a"), threading.Event())
    generate(PAYLOAD, event(token="tok-b"), threading.Event())
    marks = (tmp_path / "ran.log").read_text(encoding="utf-8").splitlines()
    assert len(marks) == 2


def test_session_pointer_is_reused_across_turns(tmp_path):
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": "ok"})
    generate = build(tmp_path)
    generate(PAYLOAD, event(token="tok-1"), threading.Event())
    generate(PAYLOAD, event(token="tok-2"), threading.Event())
    store = SessionStore(os.path.join(str(tmp_path), ".agentkit", "sessions"))
    record = store.get(driver.session_key_for("fake", PAYLOAD))
    assert record is not None
    assert record.resume_key == "fake-session-1"
    assert record.turns == 2  # 第二次是续跑，不是新会话


def test_rejected_resume_falls_back_to_a_fresh_session(tmp_path):
    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps({"answer": "重新开始也行"})
    generate = build(tmp_path)
    generate(PAYLOAD, event(token="tok-1"), threading.Event())  # 建立 session

    os.environ["FAKE_AGENT_BEHAVIOUR"] = json.dumps(
        {"answer": "重新开始也行", "reject_resume": True}
    )
    answer = generate(PAYLOAD, event(token="tok-2"), threading.Event())
    assert answer == "重新开始也行"  # 降级重试成功，用户无感
    # 但"降级过"必须留在记录里，否则上下文丢过一次就永远查不到
    runs = sorted((tmp_path / ".agentkit" / "runs").glob("*/run.json"))
    reasons = [json.loads(path.read_text(encoding="utf-8"))["outcome"]["reason"] for path in runs]
    assert "resume_rejected" in reasons
    assert "ok" in reasons


def test_describe_never_claims_and_states_the_truth(tmp_path):
    report = driver.describe("fake", home=tmp_path, workspace=str(tmp_path))
    assert report["adapter"] == "fake"
    assert report["kind"] == "other"
    assert report["binary_ok"] is True
    assert report["workspace_exists"] is True
    assert report["session_store"].endswith("sessions")


def test_unknown_agent_fails_before_doing_anything(tmp_path):
    from agentkit.errors import AdapterNotFound

    with pytest.raises(AdapterNotFound):
        driver.build_agent("nope")


# ---------------------------------------------------------------------------
# SessionStore 本身
# ---------------------------------------------------------------------------
def test_session_store_roundtrip_and_clear(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    assert store.get("chat-1") is None
    store.set("chat-1", "sess-a", adapter="fake")
    record = store.get("chat-1")
    assert record.resume_key == "sess-a" and record.turns == 1
    store.set("chat-1", "sess-b", adapter="fake")
    assert store.get("chat-1").resume_key == "sess-b"
    assert store.get("chat-1").turns == 2
    assert store.clear("chat-1") is True
    assert store.get("chat-1") is None


def test_session_store_hashes_the_filename(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    path = store.path_for("../../etc/passwd")
    assert os.path.dirname(path) == str(tmp_path / "sessions")
    assert "passwd" not in os.path.basename(path)  # 不做路径穿越


def test_session_store_file_is_private(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    store.set("chat-1", "sess-a")
    path = store.path_for("chat-1")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_consumer_entry_check_does_not_claim(tmp_path):
    """新入口的 --check 只能"看"，不能 register、不能 claim、不能建状态目录。"""
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, os.path.join(root, "agentkit-consumer.py"),
         "--agent", "fake", "--check", "--home", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
    )
    output = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, output
    assert '"adapter": "fake"' in output
    assert "no bridge event was claimed" in output
    assert not (tmp_path / "consumer-state").exists()


def test_turn_key_differs_per_claim_token():
    first = driver.turn_key_for(event(token="tok-a"))
    second = driver.turn_key_for(event(token="tok-b"))
    assert first != second
    assert first.startswith("evt-7-")
