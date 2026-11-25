"""TurnRunner 的五个契约场景 + 幂等/诚实字段。

场景定义参照 HarnessRouter 的支撑矩阵（首轮 / 续跑 / 换模型 / 产物 / 回收后续跑），
但这里用假 AGENT 跑，因此结果是确定的、跨平台的、毫秒级的。
"""

import os
import threading

import pytest

from agentkit import TurnRequest, TurnRunner
from agentkit.adapters import get_adapter
from agentkit.errors import TurnInProgress
from agentkit.idempotency import TurnStore
from agentkit.turn import TurnConfig
from agentkit.types import TurnStatus


def runner(tmp_path, behaviour=None, **cfg):
    adapter = get_adapter("fake")
    store = TurnStore(os.path.join(str(tmp_path), ".agentkit", "turns"))
    return adapter, TurnRunner(adapter, config=TurnConfig(**cfg), store=store)


def request(tmp_path, behaviour=None, **kwargs):
    metadata = {"behaviour": behaviour or {}}
    return TurnRequest(
        prompt=kwargs.pop("prompt", "请问今天有什么安排"),
        workspace=str(tmp_path),
        metadata=metadata,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 场景 1：首轮
# ---------------------------------------------------------------------------
def test_a_failed_run_reports_the_raw_tail_as_its_error(tmp_path):
    """CLI 的报错常是裸文本（不是 JSON）：解析器会忽略它，但 error 不能是空的。"""
    _adapter, turn = runner(tmp_path)
    outcome = turn.run(request(tmp_path, {"exit_code": 2, "no_final": True}))
    assert outcome.status == TurnStatus.FAILED
    assert outcome.error, "失败但 error 为空：排障时只能猜"


def test_a_secret_in_the_answer_is_redacted_at_the_boundary(tmp_path):
    """答案出站脱敏放在**底座**：adapter 不可能"忘记清洗"。"""
    _adapter, turn = runner(tmp_path)
    outcome = turn.run(request(tmp_path, {"answer": "这是密钥 sk-ant-api03-abcdefghijklmnop"}))
    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.answer == "这条回复里带有密钥或密码，已拦截，没有发出原文。"
    assert "secret_redacted" in outcome.fallbacks


def test_first_turn_completes_with_an_answer(tmp_path):
    adapter, turn = runner(tmp_path)
    outcome = turn.run(request(tmp_path, {"answer": "今天有两个会"}))
    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.answer == "今天有两个会"
    assert outcome.adapter == "fake"
    assert outcome.incomplete_reasons == []


# ---------------------------------------------------------------------------
# 场景 2：续跑（同一会话再来一轮）
# ---------------------------------------------------------------------------
def test_followup_inherits_the_resume_key(tmp_path):
    adapter, turn = runner(tmp_path)
    first = turn.run(request(tmp_path, {"answer": "第一轮"}))
    assert first.resume_key == "fake-session-1"
    second = turn.run(
        request(tmp_path, {"answer": "第二轮"}, resume_key=first.resume_key)
    )
    assert second.status == TurnStatus.COMPLETED
    assert second.answer == "第二轮"
    assert second.resume_key == "fake-session-1"


# ---------------------------------------------------------------------------
# 场景 3：换了请求里的东西，但 adapter 做不到 —— 必须如实申报
# ---------------------------------------------------------------------------
def test_unsupported_request_fields_are_reported_not_silently_dropped(tmp_path):
    """假 AGENT 什么都能做；换一个"什么都做不了"的 adapter 才能验申报。"""
    from agentkit.adapters.base import AgentAdapter
    from agentkit.adapters.registry import register
    from agentkit.types import Capabilities

    @register
    class NoFrillsAdapter(AgentAdapter):
        name = "no-frills"
        binary_name = ""
        binary_env = ""
        capabilities = Capabilities(name="no-frills", denies_tools=False)

        def resolve_binary(self, platform):
            import sys

            return sys.executable

        def build_argv(self, req, ctx):
            import json as _json

            script = (
                "import json,os\n"
                "print(json.dumps({'type': 'final', 'text': 'ok'}))\n"
            )
            return [ctx.binary, "-c", script]

        def parse_line(self, line):
            import json as _json

            try:
                payload = _json.loads(line)
            except ValueError:
                return None
            from agentkit.types import StreamEvent

            return StreamEvent(kind="result", raw=payload)

        def on_event(self, state, event):
            state.answer = (event.raw or {}).get("text")

        def classify(self, state):
            return self.make_outcome(state, TurnStatus.COMPLETED, "ok", state.answer)

    adapter = NoFrillsAdapter()
    turn = TurnRunner(adapter, config=TurnConfig())
    outcome = turn.run(
        request(tmp_path, {}, model="some-model", narrow_to=("Read",), resume_key="sess-1")
    )
    # "A silently ignored field is indistinguishable from an honoured one"
    for field in ("model", "narrow_to", "resume_key"):
        assert field in outcome.ignored, (field, outcome.ignored)
    assert outcome.status == TurnStatus.COMPLETED


def test_supported_resume_key_is_not_reported_as_ignored(tmp_path):
    adapter, turn = runner(tmp_path)  # fake 支持续跑
    outcome = turn.run(request(tmp_path, {"answer": "ok"}, resume_key="fake-session-1"))
    assert "resume_key" not in outcome.ignored


# ---------------------------------------------------------------------------
# 续跑：被拒 -> 换新会话重试一次，且必须留痕
# ---------------------------------------------------------------------------
def test_rejected_resume_is_retried_with_a_fresh_session(tmp_path):
    adapter, turn = runner(tmp_path)
    first = turn.run(request(tmp_path, {"answer": "第一轮"}))
    second = turn.run(
        request(tmp_path, {"answer": "重新开始也行", "reject_resume": True},
                resume_key=first.resume_key)
    )
    assert second.status == TurnStatus.COMPLETED
    assert second.answer == "重新开始也行"
    assert second.attempts == 2                  # 真的重试了
    assert "resume_fallback" in second.fallbacks  # 而且留下了痕迹


def test_resume_fallback_can_be_turned_off(tmp_path):
    adapter, turn = runner(tmp_path)
    first = turn.run(request(tmp_path, {"answer": "第一轮"}))
    second = turn.run(
        request(tmp_path, {"answer": "x", "reject_resume": True},
                resume_key=first.resume_key, allow_resume_fallback=False)
    )
    assert second.status == TurnStatus.FAILED
    assert second.reason == "resume_rejected"
    assert second.attempts == 1


# ---------------------------------------------------------------------------
# 输出上限按**字节**而不是按字符
# ---------------------------------------------------------------------------
def test_output_budget_counts_bytes_not_characters(tmp_path):
    """200 个汉字 = 600 字节但只有 200 个字符；上限 400 必须被字节撞破。

    按字符计数会把上限放大 3 倍（中文一点不罕见），这是最容易悄悄失效的限额。
    """
    adapter, turn = runner(tmp_path, max_output_bytes=400)
    outcome = turn.run(
        request(tmp_path, {"text": ["中" * 200], "no_final": True})
    )
    assert "output_budget" in outcome.incomplete_reasons


# ---------------------------------------------------------------------------
# 场景 4：产物
# ---------------------------------------------------------------------------
def test_artifacts_come_from_the_workspace_snapshot_not_from_self_report(tmp_path):
    adapter, turn = runner(tmp_path)
    outcome = turn.run(
        request(tmp_path, {"answer": "已生成", "create_file": "reports/today.md"})
    )
    assert "reports/today.md" in outcome.artifacts
    assert os.path.isfile(os.path.join(str(tmp_path), "reports", "today.md"))


# ---------------------------------------------------------------------------
# 场景 5：回收后续跑（同一 workspace，全新进程，产物仍在）
# ---------------------------------------------------------------------------
def test_a_recycled_workspace_still_carries_the_previous_artifacts(tmp_path):
    adapter, turn = runner(tmp_path)
    turn.run(request(tmp_path, {"answer": "第一轮", "create_file": "state.json"}))
    # 模拟"沙箱被回收后重来"：全新的 adapter/runner 实例，同一个 workspace
    adapter2, turn2 = runner(tmp_path)
    outcome = turn2.run(request(tmp_path, {"answer": "第二轮"}))
    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.artifacts == []  # 第二轮没产出新文件
    assert os.path.isfile(os.path.join(str(tmp_path), "state.json"))  # 但第一轮的还在


# ---------------------------------------------------------------------------
# 诚实字段：时间预算截断
# ---------------------------------------------------------------------------
def test_timeout_is_incomplete_never_completed(tmp_path):
    adapter, turn = runner(tmp_path)
    outcome = turn.run(
        request(tmp_path, {"sleep_s": 2.0, "answer": "迟到的答案"}, timeout_s=0.3)
    )
    assert outcome.status == TurnStatus.INCOMPLETE
    assert outcome.reason == "time_budget"
    assert "time_budget" in outcome.incomplete_reasons


def test_output_budget_stop_is_incomplete(tmp_path):
    adapter, turn = runner(tmp_path, max_output_bytes=8)
    outcome = turn.run(request(tmp_path, {"answer": "一句很长很长的回答"}))
    assert "output_budget" in outcome.incomplete_reasons


# ---------------------------------------------------------------------------
# deny 在流上执行（不信 CLI 自己会执行）
# ---------------------------------------------------------------------------
def test_denied_tool_kills_the_run(tmp_path):
    adapter, turn = runner(tmp_path)
    outcome = turn.run(request(tmp_path, {"tools": ["delete"], "answer": "不该到这"}))
    assert outcome.status == TurnStatus.FAILED
    assert outcome.reason == "denied_tool"
    assert outcome.denied_tool == "delete"
    assert outcome.answer is None


# ---------------------------------------------------------------------------
# 幂等：重复提交返回第一次的结果；在跑就等，等不到才报错
# ---------------------------------------------------------------------------
def test_same_turn_key_replays_the_first_result(tmp_path):
    adapter, turn = runner(tmp_path)
    first = turn.run(request(tmp_path, {"answer": "第一次"}, turn_key="evt-1"))
    second = turn.run(request(tmp_path, {"answer": "第二次"}, turn_key="evt-1"))
    assert first.answer == "第一次"
    assert second.replayed is True
    assert second.answer == "第一次"  # 不是"第二次"
    assert second.duration_s == first.duration_s


def test_busy_turn_raises_after_the_wait_window(tmp_path):
    adapter, turn = runner(tmp_path, wait_for_first_s=0.2)
    store = TurnStore(os.path.join(str(tmp_path), ".agentkit", "turns"))
    store.begin("evt-busy", owner="someone-else")  # 别人占着，pid 是当前进程（活着）
    with pytest.raises(TurnInProgress):
        turn.run(request(tmp_path, {"answer": "x"}, turn_key="evt-busy"))


def test_abandoned_turn_is_taken_over_after_staleness(tmp_path):
    adapter, turn = runner(tmp_path, stale_after_s=0.0)
    store = TurnStore(os.path.join(str(tmp_path), ".agentkit", "turns"))
    store.begin("evt-stale", owner="dead-process")
    outcome = turn.run(request(tmp_path, {"answer": "接管成功"}, turn_key="evt-stale"))
    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.answer == "接管成功"


# ---------------------------------------------------------------------------
# 取消与失败
# ---------------------------------------------------------------------------
def test_preexisting_stop_event_cancels_before_doing_work(tmp_path):
    adapter, turn = runner(tmp_path)
    stop = threading.Event()
    stop.set()
    outcome = turn.run(request(tmp_path, {"answer": "不该跑"}), stop_event=stop)
    assert outcome.status in (TurnStatus.CANCELLED, TurnStatus.FAILED)


def test_missing_binary_is_a_failed_outcome_not_an_exception(tmp_path):
    from agentkit.adapters.base import AgentAdapter
    from agentkit.adapters.registry import register

    @register
    class MissingBinaryAdapter(AgentAdapter):
        name = "missing-binary"
        binary_name = "definitely-not-a-real-binary-xyz"
        binary_env = ""
        from agentkit.types import Capabilities as _Caps

        capabilities = _Caps(name="missing-binary")

        def build_argv(self, req, ctx):
            return [ctx.binary]

        def parse_line(self, line):
            return None

        def classify(self, state):
            return self.make_outcome(state, TurnStatus.FAILED, "no_answer")

    adapter = MissingBinaryAdapter()
    turn = TurnRunner(adapter, config=TurnConfig())
    outcome = turn.run(request(tmp_path, {"answer": "x"}))
    # 二进制不存在 -> 必须是结构化的失败终态，而不是把异常抛给调用方
    assert outcome.status == TurnStatus.FAILED
    assert outcome.reason == "spawn_failed"
    assert "FileNotFoundError" in (outcome.error or "")


def test_relative_workspace_is_rejected_before_any_side_effect(tmp_path):
    adapter, turn = runner(tmp_path)
    req = TurnRequest(prompt="x", workspace="relative/path")
    with pytest.raises(ValueError):
        turn.run(req)


def test_artifacts_snapshot_marks_truncation_instead_of_silently_returning_nothing(tmp_path):
    """回归：工作区有 91 万文件时 snap 撞上限 → 曾静默返回空产物（假阴性）。"""
    import os

    from agentkit import artifacts as artifacts_mod

    for index in range(5):
        (tmp_path / ("f%d.txt" % index)).write_text("x")
    snap = artifacts_mod.snapshot(str(tmp_path), max_files=2)
    assert snap.truncated is True
    assert len(snap.files) == 2


def test_windowed_snapshot_ignores_old_subtrees_and_keeps_recent_ones(tmp_path):
    import os
    import time

    from agentkit import artifacts as artifacts_mod

    old = tmp_path / "old"
    old.mkdir()
    (old / "ancient.txt").write_text("old")
    stamp = time.time() - 7200
    os.utime(str(old), (stamp, stamp))
    os.utime(str(old / "ancient.txt"), (stamp, stamp))

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "new.txt").write_text("new")

    snap = artifacts_mod.snapshot(str(tmp_path), since=time.time() - 600)
    assert snap.windowed is True
    assert snap.files == {"fresh/new.txt": snap.files["fresh/new.txt"]}
    assert "fresh/new.txt" in snap.files


def test_windowed_snapshot_still_reports_new_files_and_says_it_is_windowed(tmp_path):
    """窗口模式（大工作区）仍然要报出本轮新建的文件，并用 fallbacks 说明只看过窗口。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    adapter, turn = runner(workspace, snapshot_window_s=600)
    outcome = turn.run(request(workspace, {"answer": "已生成", "create_file": "made.txt"}))
    assert outcome.status == TurnStatus.COMPLETED
    assert "made.txt" in outcome.artifacts, outcome.artifacts
    assert "artifacts_windowed" in outcome.fallbacks


def test_windowed_snapshot_does_not_claim_old_files_as_produced(tmp_path):
    """窗口模式下，很久以前就存在的文件不该被当成"本轮产物"。"""
    import time

    workspace = tmp_path / "ws"
    workspace.mkdir()
    old = workspace / "old"
    old.mkdir()
    (old / "ancient.md").write_text("x")
    stamp = time.time() - 7200
    os.utime(str(old), (stamp, stamp))
    os.utime(str(old / "ancient.md"), (stamp, stamp))
    adapter, turn = runner(workspace, snapshot_window_s=600)
    outcome = turn.run(request(workspace, {"answer": "没产出"}))
    assert outcome.artifacts == []
    assert "ancient.md" not in " ".join(outcome.artifacts)
