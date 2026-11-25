"""注册/生效/重载/停用逻辑的单元测试（全部注入假 launchctl，不碰真实服务）。

这里钉住的四件事，恰好是"换 release 之后到底跑的是哪份代码"最容易出错的地方：

1. ``start`` 幂等：已加载就不重复 bootstrap；
2. ``restart`` 必须是 bootout+bootstrap —— 只 kickstart 会跑 launchd 缓存的旧路径；
3. ``stop`` 把 plist 挪进 ``disabled-consumers/``，登录时 launchd 不会把它偷偷拉起来；
4. 旧的 Cursor 引擎已整体删除，任何地方都不该再引用它的 label。
"""

import json
import plistlib
import sys

import pytest

from agentkit import rollout


@pytest.fixture(autouse=True)
def _never_fall_back_to_the_real_home(monkeypatch):
    """单测不许摸真实安装目录：`_default_home()` 会让 `_unpark()` 真的把线上 parked
    plist 挪进临时目录（真的发生过）。需要 home 的用例一律显式传。"""
    monkeypatch.setattr(rollout, "_default_home", lambda: None)


class Result(object):
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeLaunchd(object):
    """最小的 launchd 状态机：print / bootstrap / bootout / kickstart。"""

    def __init__(self, loaded=(), bootstrap_ok=True):
        self.loaded = set(loaded)
        self.bootstrap_ok = bootstrap_ok
        self.calls = []

    def labels(self, subcommand):
        out = []
        for call in self.calls:
            if len(call) > 1 and call[1] == subcommand:
                out.append(call[-1] if subcommand == "bootstrap" else call[2])
        return out

    def __call__(self, argv, timeout=60):
        argv = list(argv)
        self.calls.append(argv)
        command = argv[1]
        if command == "print":
            label = argv[2].rsplit("/", 1)[-1]
            if label in self.loaded:
                return Result(0, "pid = 4242\nstate = running\n", "")
            return Result(113, "", "Could not find service")
        if command == "bootout":
            label = argv[2].rsplit("/", 1)[-1]
            self.loaded.discard(label)
            return Result(0)
        if command == "bootstrap":
            if not self.bootstrap_ok:
                return Result(1, "", "Bootstrap failed: 5: Input/output error")
            payload = plistlib.loads(open(argv[3], "rb").read())
            self.loaded.add(payload["Label"])
            return Result(0)
        if command == "kickstart":
            label = argv[-1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return Result(113, "", "Could not find service")
            return Result(0)
        raise AssertionError("unexpected launchctl call: %s" % argv)

    def commands(self):
        return [call[1] for call in self.calls if call[1] != "print"]


def write_plist(tmp_path, agent="cursor", consumer="agentkit-cursor", kind="other",
                home="/tmp/home", workspace=None):
    argv = ["/usr/bin/python3", "/tmp/home/current/agentkit-consumer.py",
            "--agent", agent, "--consumer", consumer, "--kind", kind, "--home", home]
    if workspace:
        argv += ["--workspace", workspace]
    path = tmp_path / (rollout.AGENTKIT_LABEL + ".plist")
    path.write_bytes(plistlib.dumps({"Label": rollout.AGENTKIT_LABEL, "ProgramArguments": argv}))
    return path


def test_read_plist_recovers_the_consumer_identity(tmp_path):
    path = write_plist(tmp_path, workspace="/home/示例/Documents")
    info = rollout.read_plist(path)
    assert info["label"] == rollout.AGENTKIT_LABEL
    assert info["agent"] == "cursor"
    assert info["consumer"] == "agentkit-cursor"
    assert info["kind"] == "other"
    assert info["workspace"] == "/home/示例/Documents"


def test_missing_plist_tells_you_to_run_install(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        rollout.read_plist(tmp_path / "nope.plist")
    assert "--with-agentkit" in str(excinfo.value)


def test_retired_cursor_label_is_gone_from_the_module():
    """引擎 A 已删除：模块里留一个 cursor label，就意味着还有代码路径指向死掉的入口。"""
    assert not hasattr(rollout, "CURSOR_LABEL")


@pytest.mark.parametrize("loaded", [set(), {rollout.AGENTKIT_LABEL}])
def test_start_is_idempotent(tmp_path, loaded):
    path = write_plist(tmp_path)
    launchd = FakeLaunchd(loaded=loaded)
    result = rollout.start(path, run=launchd)
    assert len(launchd.labels("bootstrap")) <= 1
    if rollout.AGENTKIT_LABEL in loaded:
        assert result["steps"] == ["already-loaded"]
    launchd_again = rollout.start(path, run=launchd)
    assert launchd_again["steps"] == ["already-loaded"]


def test_bootstrap_failure_is_reported_not_swallowed(tmp_path):
    path = write_plist(tmp_path)
    launchd = FakeLaunchd(loaded=set(), bootstrap_ok=False)
    with pytest.raises(RuntimeError) as excinfo:
        rollout.start(path, run=launchd)
    assert "bootstrap" in str(excinfo.value)


def test_status_reports_the_agentkit_consumer_only(tmp_path):
    path = write_plist(tmp_path)
    launchd = FakeLaunchd(loaded={rollout.AGENTKIT_LABEL})
    data = rollout.status(path, run=launchd)
    assert data["agentkit"]["loaded"] is True
    assert "cursor" not in data
    assert data["consumer"] == "agentkit-cursor"
    assert data["plist_present"] is True


def test_stop_parks_the_plist_so_login_cannot_resurrect_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = write_plist(tmp_path, home=str(home))
    monkeypatch.setattr(rollout, "_service_dir", lambda: tmp_path)
    monkeypatch.setattr(rollout, "plist_path", lambda home=None: path)
    launchd = FakeLaunchd(loaded={rollout.AGENTKIT_LABEL})
    result = rollout.stop(run=launchd)
    assert rollout.AGENTKIT_LABEL not in launchd.loaded
    assert result["steps"][0] == "bootout:%s" % rollout.AGENTKIT_LABEL
    assert "park:%s.plist" % rollout.AGENTKIT_LABEL in result["steps"]
    # 停用的 plist 必须离开 LaunchAgents（否则重启后又自己起来）
    assert not (tmp_path / (rollout.AGENTKIT_LABEL + ".plist")).exists()
    assert (home / rollout.PARKED_DIR / (rollout.AGENTKIT_LABEL + ".plist")).is_file()


def test_stop_on_a_stopped_service_is_a_no_op(tmp_path, monkeypatch):
    path = write_plist(tmp_path)
    monkeypatch.setattr(rollout, "plist_path", lambda home=None: path)
    launchd = FakeLaunchd(loaded=set())
    result = rollout.stop(run=launchd)
    assert "already-stopped" in result["steps"]
    assert launchd.commands() == []


def test_start_unparks_its_own_plist_after_a_previous_stop(tmp_path, monkeypatch):
    """stop 会把 plist 挪出 LaunchAgents；再 start 必须先把文件挪回来，否则 bootstrap 找不到。"""
    home = tmp_path / "home"
    home.mkdir()
    path = write_plist(tmp_path, home=str(home))
    monkeypatch.setattr(rollout, "_service_dir", lambda: tmp_path)
    launchd = FakeLaunchd(loaded={rollout.AGENTKIT_LABEL})
    rollout.stop(run=launchd)                    # 停 + 挪走 plist
    assert not path.is_file()
    result = rollout.start(path, run=launchd, home=str(home))  # 再起：应先把 plist 挪回
    assert path.is_file()
    assert rollout.AGENTKIT_LABEL in launchd.loaded
    assert any(step.startswith("unpark:") for step in result["steps"])


def test_park_refuses_a_non_absolute_home(tmp_path):
    """回归：单测里 home 传了 None 时，曾经在当前目录建出一个 `None/` 目录。"""
    path = write_plist(tmp_path, home=str(tmp_path / "home"))
    with pytest.raises(RuntimeError) as excinfo:
        rollout._park(path, "")
    assert "绝对的 home" in str(excinfo.value)
    with pytest.raises(RuntimeError):
        rollout._park(path, "None")


def test_tests_cannot_reach_the_real_install(tmp_path):
    """`_default_home` 被 autouse fixture 关掉：找不到 parked plist 时必须报错，而不是去真实 home 找。"""
    assert rollout._default_home() is None
    with pytest.raises(SystemExit):
        rollout.start(tmp_path / "missing.plist", run=FakeLaunchd())


def test_register_only_registers_never_claims(tmp_path):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    bridge = home / "bin" / "feishu-bridge"
    bridge.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    def fake(argv, timeout=60):
        calls.append(list(argv))
        return Result(0, json.dumps({"ok": True, "created": True}), "")

    receipt = rollout.register(str(home), "agentkit-cursor", "other", run=fake)
    assert receipt["ok"] is True
    assert calls == [[str(bridge), "events", "register",
                      "--consumer", "agentkit-cursor", "--kind", "other"]]
    assert all("claim" not in part for part in calls[0])


# ---------------------------------------------------------------------------
# restart：今天在真机上踩到的"install 换了 release，进程还在跑旧代码"
# ---------------------------------------------------------------------------
def test_restart_boots_out_then_bootstraps_so_new_plist_wins(tmp_path, monkeypatch):
    """kickstart -k 复用 launchd 缓存的 ProgramArguments，install.py 改写 plist 后
    进程仍指向旧 release。restart 必须 bootout → bootstrap → kickstart。"""
    path = write_plist(tmp_path)
    monkeypatch.setattr(rollout, "plist_path", lambda home=None: path)
    launchd = FakeLaunchd(loaded={rollout.AGENTKIT_LABEL})
    result = rollout.restart(path, run=launchd)
    assert launchd.commands() == ["bootout", "bootstrap", "kickstart"], launchd.commands()
    assert result["restarted"] is True
    assert result["steps"] == ["bootout:%s" % rollout.AGENTKIT_LABEL,
                              "bootstrap:%s" % path.name,
                              "kickstart:%s" % rollout.AGENTKIT_LABEL]


def test_restart_starts_a_stopped_consumer(tmp_path, monkeypatch):
    """没有别的 consumer 可抢事件了：没在跑就直接 bootstrap，不需要 take-over。"""
    path = write_plist(tmp_path)
    monkeypatch.setattr(rollout, "plist_path", lambda home=None: path)
    launchd = FakeLaunchd(loaded=set())
    result = rollout.restart(path, run=launchd)
    assert launchd.commands() == ["bootstrap", "kickstart"], launchd.commands()
    assert rollout.AGENTKIT_LABEL in launchd.loaded
    assert result["restarted"] is True


def test_restart_fails_loudly_when_the_service_will_not_stop(tmp_path, monkeypatch):
    path = write_plist(tmp_path)
    monkeypatch.setattr(rollout, "plist_path", lambda home=None: path)
    monkeypatch.setattr(rollout, "LOAD_TIMEOUT_S", 0.01)

    class StuckLaunchd(FakeLaunchd):
        def __call__(self, argv, timeout=60):
            if list(argv)[1] == "bootout":
                self.calls.append(list(argv))
                return Result(0)          # bootout "成功" 但服务其实还在
            return FakeLaunchd.__call__(self, argv, timeout)

    launchd = StuckLaunchd(loaded={rollout.AGENTKIT_LABEL})
    with pytest.raises(RuntimeError) as excinfo:
        rollout.restart(path, run=launchd)
    assert "没有停下来" in str(excinfo.value)
    assert "bootstrap" not in launchd.commands()
