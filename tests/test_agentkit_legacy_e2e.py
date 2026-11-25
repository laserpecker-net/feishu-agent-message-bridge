"""三个真实 adapter 的端到端验证（用假 CLI 重放真实 JSONL 形状，不联网、不调模型）。

这一组是整个自查里最有价值的部分：它证明"legacy 的 argv/env/流协议/deny/答案提取"
在 agentkit 里**真的能跑通**，而不只是单测里形状相等。

每个用例都用 ``#!<当前解释器>`` 的假二进制（与 ``tests/test_codex_consumer.py`` 同法），
因此三个 adapter 的 argv 组装、stdin 投递、环境隔离、状态机、拒绝路径都被真实执行了一遍。
"""

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentkit import TurnRequest, TurnRunner  # noqa: E402
from agentkit.adapters import get_adapter  # noqa: E402
from agentkit.types import TurnStatus  # noqa: E402


def fake_cli(directory, body, name="fake-cli"):
    path = pathlib.Path(directory) / name
    path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def run(adapter_name, workspace, prompt="你好世界", timeout_s=30):
    return TurnRunner(get_adapter(adapter_name)).run(
        TurnRequest(prompt=prompt, workspace=str(workspace), timeout_s=timeout_s)
    )


def seen(workspace, name="seen.json"):
    """读假 CLI 写的现场记录（它的 cwd 可能是工作区，也可能是私有 run 目录）。"""
    direct = pathlib.Path(workspace) / name
    if direct.is_file():
        return json.loads(direct.read_text(encoding="utf-8"))
    candidates = sorted(pathlib.Path(workspace, ".agentkit", "runs").glob("*/" + name))
    assert candidates, "fake CLI wrote no %s anywhere" % name
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
CODEX_PRELUDE = (
    "import json,os,pathlib,sys\n"
    "prompt = sys.stdin.read()\n"
    "pathlib.Path('seen.json').write_text(json.dumps({"
    "'argv': sys.argv[1:], 'stdin': prompt, 'cwd': str(pathlib.Path.cwd()),"
    "'codex_home': os.environ.get('CODEX_HOME'), 'home': os.environ.get('HOME')}))\n"
    "def emit(**kw):\n"
    "    print(json.dumps(kw), flush=True)\n"
)


def codex(monkeypatch, tmp_path, body):
    auth = tmp_path / "real-codex-home"
    auth.mkdir(exist_ok=True)
    (auth / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(auth))
    binary = fake_cli(tmp_path, CODEX_PRELUDE + body)
    monkeypatch.setenv("CODEX_CONSUMER_BINARY", binary)
    return binary


def test_codex_success_end_to_end(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex(
        monkeypatch,
        tmp_path,
        "emit(type='thread.started', thread_id='thread-1')\n"
        "emit(type='item.completed', item={'type': 'reasoning', 'text': '想一下'})\n"
        "emit(type='item.completed', item={'type': 'agent_message',"
        " 'text': json.dumps({'answer': '回答:' + prompt.strip()[:2]}, ensure_ascii=False)})\n"
        "emit(type='turn.completed', usage={'output_tokens': 7})\n",
    )
    outcome = run("codex", workspace)
    assert outcome.status == TurnStatus.COMPLETED, outcome
    assert outcome.answer == "回答:你好"
    assert outcome.resume_key == "thread-1"
    assert outcome.usage == {"output_tokens": 7}

    record = seen(workspace)
    assert record["cwd"] == str(workspace)          # codex 的 cwd 就是工作区
    assert record["stdin"] == "你好世界"             # prompt 走 stdin，不是 argv
    assert record["home"].startswith(str(workspace))  # HOME 已重定向进工作区
    assert record["codex_home"].endswith("codex-home")  # 登录态被隔离到 run 目录
    assert "--output-schema" in record["argv"]
    assert record["argv"][-1] == "-"


def test_codex_denied_item_kills_the_run_fast(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex(
        monkeypatch,
        tmp_path,
        "emit(type='thread.started', thread_id='t')\n"
        "emit(type='item.started', item={'type': 'mcp_tool_call'})\n"
        "import time; time.sleep(30)\n",
    )
    import time

    started = time.monotonic()
    outcome = run("codex", workspace)
    assert time.monotonic() - started < 8
    assert outcome.status == TurnStatus.FAILED
    assert outcome.reason == "denied_tool"
    assert outcome.denied_tool == "mcp_tool_call"


def test_codex_missing_terminal_event_fails_closed(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex(
        monkeypatch,
        tmp_path,
        "emit(type='thread.started', thread_id='t')\n"
        "emit(type='item.completed', item={'type': 'agent_message',"
        " 'text': json.dumps({'answer': '看起来像答案'})})\n",
    )
    outcome = run("codex", workspace)
    # 没有 turn.completed -> 不许当成成功
    assert outcome.status == TurnStatus.FAILED


def test_codex_unknown_event_type_fails_closed(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex(monkeypatch, tmp_path, "emit(type='who_knows', payload=1)\n")
    outcome = run("codex", workspace)
    assert outcome.status == TurnStatus.FAILED


def test_codex_answer_with_extra_keys_is_rejected(monkeypatch, tmp_path):
    """legacy 要求答案 JSON **恰好**只有 ``answer`` 键。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex(
        monkeypatch,
        tmp_path,
        "emit(type='thread.started', thread_id='t')\n"
        "emit(type='item.completed', item={'type': 'agent_message',"
        " 'text': json.dumps({'answer': 'ok', 'extra': 'x'})})\n"
        "emit(type='turn.completed', usage={})\n",
    )
    outcome = run("codex", workspace)
    assert outcome.status == TurnStatus.FAILED


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------
def cursor(monkeypatch, tmp_path, body):
    binary = fake_cli(tmp_path, body)
    monkeypatch.setenv("CURSOR_AGENT_BINARY", binary)
    return binary


CURSOR_BODY = (
    "import json,pathlib,sys\n"
    "prompt = sys.argv[-1]\n"
    "cwd = pathlib.Path.cwd()\n"
    "policy = cwd / '.cursor' / 'cli.json'\n"
    "pathlib.Path('seen.json').write_text(json.dumps({"
    "'argv': sys.argv[1:], 'cwd': str(cwd), 'prompt': prompt,"
    "'policy_exists': policy.is_file(),"
    "'policy': json.loads(policy.read_text()) if policy.is_file() else None}))\n"
    "def emit(**kw):\n"
    "    print(json.dumps(kw), flush=True)\n"
)


CURSOR_HOME_PROBE = (
    "import json,os,pathlib\n"
    "pathlib.Path('home.json').write_text(json.dumps({'home': os.environ.get('HOME'),"
    "'tmpdir': os.environ.get('TMPDIR'), 'pwd': os.environ.get('PWD')}))\n"
)


def test_cursor_success_end_to_end(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cursor(
        monkeypatch,
        tmp_path,
        CURSOR_HOME_PROBE
        + CURSOR_BODY
        + "emit(type='system', subtype='init', session_id='sess-1', model='gpt-5')\n"
        "emit(type='assistant', text='先想想')\n"
        "emit(type='result', subtype='success', is_error=False,"
        " result=json.dumps({'answer': '游标答案'}, ensure_ascii=False))\n",
    )
    outcome = run("cursor", workspace)
    assert outcome.status == TurnStatus.COMPLETED, outcome
    assert outcome.answer == "游标答案"
    assert outcome.served_model == "gpt-5"
    assert outcome.resume_key == "sess-1"

    record = seen(workspace)
    # 1) cwd 是私有 run 目录（不是用户工作区）
    assert ".agentkit/runs/" in record["cwd"].replace(os.sep, "/")
    # 2) 静态策略文件真的写出来了，内容与 legacy 同源
    from agentkit.adapters.legacy import ALLOW, DENY

    assert record["policy"] == {"permissions": {"allow": list(ALLOW), "deny": list(DENY)}}
    # 3) 工作区没有被 .cursor 污染
    assert not (workspace / ".cursor").exists()
    # 4) 工作区通过 --add-dir 暴露，prompt 走 argv 末位
    assert "--add-dir" in record["argv"] and str(workspace) in record["argv"]
    assert record["prompt"] == "你好世界"
    # 5) HOME 必须是真实 $HOME（登录态在那里；实测重定向即 Not logged in），临时目录仍收进工作区
    probe = json.loads((pathlib.Path(record["cwd"]) / "home.json").read_text(encoding="utf-8"))
    assert probe["home"] == os.environ.get("HOME")
    assert probe["tmpdir"].startswith(str(workspace))


def test_cursor_failed_result_subtype_is_not_an_answer(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cursor(
        monkeypatch,
        tmp_path,
        CURSOR_BODY
        + "emit(type='system', subtype='init', session_id='s')\n"
        "emit(type='result', subtype='error', is_error=False,"
        " result=json.dumps({'answer': '假装成功'}))\n",
    )
    outcome = run("cursor", workspace)
    assert outcome.status == TurnStatus.FAILED


def test_cursor_answer_must_be_json_object(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cursor(
        monkeypatch,
        tmp_path,
        CURSOR_BODY
        + "emit(type='system', subtype='init', session_id='s')\n"
        "emit(type='result', subtype='success', is_error=False, result='纯文本没有 JSON')\n",
    )
    outcome = run("cursor", workspace)
    assert outcome.status == TurnStatus.FAILED


# ---------------------------------------------------------------------------
# DSH
# ---------------------------------------------------------------------------
DSH_BODY = (
    "import json,os,pathlib,sys\n"
    "args = sys.argv[1:]\n"
    "overlay = pathlib.Path(args[args.index('--patch') + 1])\n"
    "prompt = sys.stdin.read()\n"
    "cwd = pathlib.Path.cwd()\n"
    "pathlib.Path('seen.json').write_text(json.dumps({"
    "'argv': args, 'cwd': str(cwd), 'stdin': prompt,"
    "'overlay_exists': overlay.is_file(),"
    "'overlay': overlay.read_text() if overlay.is_file() else None,"
    "'permission_mode': os.environ.get('DSH_PERMISSION_MODE'),"
    "'dsh_home': os.environ.get('DSH_HOME'), 'home': os.environ.get('HOME')}))\n"
    "def emit(**kw):\n"
    "    print(json.dumps(kw), flush=True)\n"
)


def dsh(monkeypatch, tmp_path, body):
    binary = fake_cli(tmp_path, body)
    monkeypatch.setenv("DSH_AGENT_BINARY", binary)
    return binary


def real_dsh_home(tmp_path):
    """造一个"用户的真实 DSH 家目录"，里面有一份凭据（绝不碰真的 ~/.dsh）。"""
    home = tmp_path / "real-dsh-home"
    home.mkdir(exist_ok=True)
    (home / ".credentials.yaml").write_text("deepseek: token-placeholder\n", encoding="utf-8")
    return home


def test_dsh_success_end_to_end(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='dsh-1', cwd=str(cwd))\n"
        "emit(type='status', phase='turn_start')\n"
        "emit(type='text', text='检索中')\n"
        "emit(type='status', phase='turn_end', reason={'kind': 'completed'})\n"
        "emit(type='final', text=json.dumps({'answer': 'DSH答案'}, ensure_ascii=False))\n",
    )
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.COMPLETED, outcome
    assert outcome.answer == "DSH答案"
    assert outcome.resume_key == "dsh-1"

    record = seen(workspace)
    assert record["stdin"] == "你好世界"            # prompt 走 stdin
    assert record["permission_mode"] == "read-only"  # 与 legacy 一致的兜底
    assert record["overlay_exists"] is True
    # DSH_HOME 必须指向本次 run 的私有目录（HOME 重定向对它无效，已实测）
    assert record["dsh_home"] and ".agentkit/runs/" in record["dsh_home"].replace(os.sep, "/")
    overlay = record["overlay"]
    for tool in ("tool-subagent", "tool-workflow"):
        assert "id: %s" % tool in overlay and "disabled: true" in overlay
    assert record["argv"][:2] == ["headless", "--patch"]
    # 没有用户补丁时 argv 只有本仓那一层 --patch（机器级补丁是可选叠加）
    assert record["argv"].count("--patch") == 1
    assert record["argv"][-2:] == ["--json", "-"]


def test_dsh_home_is_isolated_and_credentials_are_linked_not_copied(monkeypatch, tmp_path):
    """DSH 用 passwd 解析家目录：只能靠 DSH_HOME 改道，凭据用符号链接接进来。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real = real_dsh_home(tmp_path)
    monkeypatch.setenv("DSH_HOME", str(real))
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='d1', cwd=str(cwd))\n"
        "emit(type='status', phase='turn_end', reason={'kind': 'completed'})\n"
        "emit(type='final', text=json.dumps({'answer': 'ok'}))\n",
    )
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.COMPLETED

    private = pathlib.Path(seen(workspace)["dsh_home"])
    assert private != real
    assert ".agentkit/runs/" in str(private).replace(os.sep, "/")
    linked = private / ".credentials.yaml"
    assert linked.is_symlink() and linked.resolve() == (real / ".credentials.yaml").resolve()


def test_dsh_user_patch_is_appended_as_a_second_patch_layer(monkeypatch, tmp_path):
    """非官方 provider 的机器：用户 DSH home 里的补丁作为第二层 --patch 传进去。

    这层的存在意义是"headless 不能只靠 DSH profile"：providers/model 的机器配置由
    用户在 `$DSH_HOME/feishu-consumer.patch.yml` 给出，消费者只负责原样叠加。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real = real_dsh_home(tmp_path)
    extra = real / "feishu-consumer.patch.yml"
    extra.write_text("- id: agent-default-model\n  config:\n    model: m\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(real))
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='d2', cwd=str(cwd))\n"
        "emit(type='status', phase='turn_end', reason={'kind': 'completed'})\n"
        "emit(type='final', text=json.dumps({'answer': 'ok'}))\n",
    )
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.COMPLETED

    argv = seen(workspace)["argv"]
    assert argv.count("--patch") == 2
    assert str(extra) in argv
    assert argv[-2:] == ["--json", "-"]


def test_dsh_user_env_reaches_the_child_while_policy_keys_win(monkeypatch, tmp_path):
    """机器级 env 文件：内部 CA 这类变量必须进子进程；沙箱/私有 home 不可被覆盖。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real = real_dsh_home(tmp_path)
    (real / "feishu-consumer.env").write_text(
        "NODE_EXTRA_CA_CERTS=/tmp/example-root-ca.crt\n"
        "export DSH_PERMISSION_MODE=workspace-write\n"
        "DSH_HOME=/tmp/not-allowed\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DSH_HOME", str(real))
    body = (
        "import json,os,pathlib,sys\n"
        "prompt = sys.stdin.read()\n"
        "pathlib.Path('seen.json').write_text(json.dumps({"
        "'ca': os.environ.get('NODE_EXTRA_CA_CERTS'),"
        "'permission_mode': os.environ.get('DSH_PERMISSION_MODE'),"
        "'dsh_home': os.environ.get('DSH_HOME'), 'stdin': prompt}))\n"
        "def emit(**kw):\n"
        "    print(json.dumps(kw), flush=True)\n"
        "emit(type='session', sessionId='d3', cwd=os.getcwd())\n"
        "emit(type='status', phase='turn_end', reason={'kind': 'completed'})\n"
        "emit(type='final', text=json.dumps({'answer': 'ok'}))\n"
    )
    dsh(monkeypatch, tmp_path, body)
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.COMPLETED

    record = seen(workspace)
    assert record["ca"] == "/tmp/example-root-ca.crt"
    assert record["permission_mode"] == "read-only"   # 机器环境改不掉策略键
    assert record["dsh_home"] and ".agentkit/runs/" in record["dsh_home"].replace(os.sep, "/")


def test_dsh_thinking_and_tool_result_are_benign(monkeypatch, tmp_path):
    """真机踩过（2026-11-05）：真实模型会发 thinking / tool_result。

    它们没有 ``phase``，一旦落进 status 分支就被判 ``invalid dsh status``，
    答案明明已经生成却整轮失败。legacy 入口对这两类事件本来就"只落盘不做动作"，
    agentkit 必须给出同一个答案：放行。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='d4', cwd=str(cwd))\n"
        "emit(type='status', phase='turn_start', turn=1)\n"
        "emit(type='status', phase='step_start', turn=1, step=1)\n"
        "emit(type='thinking', text='想一下')\n"
        "emit(type='tool_call', callId='c1', tool='read', input={'path': '/tmp/x'})\n"
        "emit(type='tool_result', callId='c1', status='ok', result='data')\n"
        "emit(type='text', text='中间说明')\n"
        "emit(type='status', phase='step_end', turn=1, step=1, "
        "usage={'inputTokens': 10, 'outputTokens': 5})\n"
        "emit(type='status', phase='turn_end', turn=1, reason={'kind': 'completed'})\n"
        "emit(type='final', text=json.dumps({'answer': '真实答案'}, ensure_ascii=False))\n",
    )
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.COMPLETED, outcome
    assert outcome.answer == "真实答案"


def test_cursor_editor_binary_is_refused(monkeypatch, tmp_path):
    """`cursor` 是编辑器 CLI：指错了必须立刻失败，不能弹出窗口。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    editor = pathlib.Path(tmp_path) / "cursor"
    editor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    editor.chmod(0o700)
    monkeypatch.setenv("CURSOR_AGENT_BINARY", str(editor))
    outcome = run("cursor", workspace)
    assert outcome.status == TurnStatus.FAILED
    assert "editor" in (outcome.error or "")


def test_dsh_final_without_turn_end_fails_closed(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='dsh-1', cwd=str(cwd))\n"
        "emit(type='status', phase='turn_start')\n"
        "emit(type='final', text=json.dumps({'answer': '没跑完就给答案'}))\n",
    )
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.FAILED


def test_dsh_unknown_event_type_fails_closed(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dsh(monkeypatch, tmp_path, DSH_BODY + "emit(type='brand_new_event')\n")
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.FAILED


def test_dsh_session_workspace_mismatch_fails_closed(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='dsh-1', cwd='/somewhere/else')\n"
        "emit(type='status', phase='turn_end', reason={'kind': 'completed'})\n"
        "emit(type='final', text=json.dumps({'answer': 'x'}))\n",
    )
    outcome = run("dsh", workspace)
    assert outcome.status == TurnStatus.FAILED


def test_dsh_denied_tool_kills_the_run(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dsh(
        monkeypatch,
        tmp_path,
        DSH_BODY
        + "emit(type='session', sessionId='dsh-1', cwd=str(cwd))\n"
        "emit(type='status', phase='turn_start')\n"
        "emit(type='tool_call', tool='subagent', input={})\n"
        "import time; time.sleep(30)\n",
    )
    import time

    started = time.monotonic()
    outcome = run("dsh", workspace)
    assert time.monotonic() - started < 8
    assert outcome.status == TurnStatus.FAILED
    assert outcome.reason == "denied_tool"
