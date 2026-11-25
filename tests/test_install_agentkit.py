"""install.py --with-agentkit：把 agentkit 打包进 release 并写一个**不加载**的 plist。

三条要求：

1. 不传 ``--with-agentkit`` 时，行为与旧安装**逐字节一致**（不多写任何文件）；
2. 传了就必须在 preflight 里校验齐全，缺文件**在任何写操作之前**失败；
3. plist 只写不加载 —— 注册与生效是两步，接管线上是人的决定。
"""

import os
import plistlib
from pathlib import Path
from unittest.mock import patch

import pytest

import install


def make_source(root):
    bridge = Path(root) / "bridge"
    bridge.mkdir(parents=True)
    for name, text in {"__init__.py": "", "agent.py": "import os\n",
                       "cli.py": "pass\n", "host.py": "pass\n", "release.py": "pass\n",
                       "store.py": "pass\n", "transport.py": "pass\n",
                       "service.py": "pass\n", "consumer.py": "pass\n",
                       "consumerctl.py": "pass\n", "knowledge.py": "pass\n",
                       "gateway.py": "pass\n"}.items():
        (bridge / name).write_text(text)
    for name in install.AGENTKIT_FILES + install.CONSUMER_FILES:
        (Path(root) / name).write_text("# %s\n" % name)
    adapters = Path(root) / "agentkit" / "adapters"
    adapters.mkdir(parents=True)
    (adapters / "legacy.py").write_text("# adapters\n")
    (adapters / "__init__.py").write_text("")
    (Path(root) / "agentkit" / "__init__.py").write_text("")
    return Path(root)


class Fixture(object):
    def __init__(self, root):
        self.root = Path(root)
        self.source = make_source(self.root / "source")
        self.home = self.root / "home"
        self.plist = self.root / "LaunchAgents" / (install.LABEL + ".plist")
        self.link = self.root / "bin" / "feishu-bridge"
        self.lark = self.root / "lark-cli"
        self.lark.write_text("#!/bin/sh\nexit 0\n")
        self.cursor = self.root / "cursor-agent"
        self.cursor.write_text("#!/bin/sh\nexit 0\n")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def install(self, **kwargs):
        with patch.dict(os.environ, {"TARGET_OPEN_ID": "ou_fixture"}):
            return install.install(source=self.source, home=self.home, plist=self.plist,
                                   link=self.link, lark_binary=self.lark, **kwargs)

    def release(self):
        return self.home / "releases" / install._release_id(self.source)

    def agentkit_plist(self):
        return self.plist.with_name(install.AGENTKIT_LABEL + ".plist")


def test_default_install_writes_no_agentkit_artifacts(tmp_path):
    fixture = Fixture(tmp_path)
    receipt = fixture.install()
    assert "agentkit" not in receipt
    assert not fixture.agentkit_plist().exists()
    assert not (fixture.release() / "agentkit").exists()
    assert not (fixture.release() / "agentkit-consumer.py").exists()


def test_with_agentkit_packages_the_runtime_into_the_release(tmp_path):
    fixture = Fixture(tmp_path)
    receipt = fixture.install(with_agentkit=True, agentkit_agent="cursor",
                              agentkit_consumer="agentkit-cursor",
                              agentkit_workspace=str(fixture.workspace))
    release = fixture.release()
    assert (release / "agentkit" / "adapters" / "legacy.py").is_file()
    for name in install.AGENTKIT_FILES:
        assert (release / name).is_file(), name
    # 入口脚本必须可执行（launchd 用 python 跑，但人工排查也要能直接执行）
    assert os.access(str(release / "agentkit-consumer.py"), os.X_OK)
    agentkit = receipt["agentkit"]
    assert agentkit["consumer"] == "agentkit-cursor"
    assert agentkit["agent"] == "cursor"
    assert agentkit["release"] == str(release)
    assert agentkit["release_files"]["agentkit-consumer.py"]
    assert agentkit["release_files"]["agentkit/adapters/legacy.py"]


def test_plist_is_written_but_not_loaded(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.install(with_agentkit=True, agentkit_workspace=str(fixture.workspace))
    payload = plistlib.loads(fixture.agentkit_plist().read_bytes())
    assert payload["Label"] == install.AGENTKIT_LABEL
    argv = payload["ProgramArguments"]
    assert argv[0] == install.sys.executable
    assert argv[1].endswith("agentkit-consumer.py")
    assert "--consumer" in argv and argv[argv.index("--consumer") + 1] == "agentkit-cursor"
    assert "--workspace" in argv and argv[argv.index("--workspace") + 1] == str(fixture.workspace)
    # RunAtLoad 是给 launchd 的，不代表安装期加载过：install 里没有任何 launchctl 调用
    assert payload["RunAtLoad"] is True


def test_missing_agentkit_sources_fail_before_any_write(tmp_path):
    fixture = Fixture(tmp_path)
    (fixture.source / "agentkit-consumer.py").unlink()
    with pytest.raises(RuntimeError) as excinfo:
        fixture.install(with_agentkit=True)
    assert "agentkit-consumer.py" in str(excinfo.value)
    assert not fixture.home.exists()


def test_fake_adapter_cannot_be_installed_as_consumer(tmp_path):
    """fake 只给平台自检用；装成常驻消费者会让假答案 claim 真实飞书消息。"""
    fixture = Fixture(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        fixture.install(with_agentkit=True, agentkit_agent="fake")
    assert "未知 agentkit agent" in str(excinfo.value)
    assert not fixture.home.exists()


def test_unknown_agent_is_rejected(tmp_path):
    fixture = Fixture(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        fixture.install(with_agentkit=True, agentkit_agent="claude")
    assert "未知 agentkit agent" in str(excinfo.value)
    assert not fixture.home.exists()


def test_workspace_must_exist(tmp_path):
    fixture = Fixture(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        fixture.install(with_agentkit=True, agentkit_workspace=str(tmp_path / "nope"))
    assert "workspace" in str(excinfo.value)


def test_reinstall_is_idempotent_for_agentkit(tmp_path):
    fixture = Fixture(tmp_path)
    first = fixture.install(with_agentkit=True)
    first_plist = plistlib.loads(fixture.agentkit_plist().read_bytes())
    second = fixture.install(with_agentkit=True)
    second_plist = plistlib.loads(fixture.agentkit_plist().read_bytes())
    assert first["agentkit"]["release_files"] == second["agentkit"]["release_files"]
    assert first_plist["ProgramArguments"] == second_plist["ProgramArguments"]
    assert (fixture.release() / "agentkit-consumer.py").is_file()


def test_agentkit_ctl_is_shipped_into_the_release(tmp_path):
    """`agentkit-consumerctl.py` 是"换 release / 回滚"的入口，必须随 release 落到盘上。

    以前它只在仓库里能跑：装好的机器上想回滚得先去拉一份仓库，这是运维缺口。
    """
    fixture = Fixture(tmp_path)
    receipt = fixture.install(with_agentkit=True)
    ctl = fixture.release() / "agentkit-consumerctl.py"
    assert ctl.is_file(), "release 里没有 agentkit-consumerctl.py"
    assert ctl.stat().st_mode & 0o111, "ctl 必须可执行"
    assert receipt["agentkit"]["release_files"].get("agentkit-consumerctl.py")

def test_parked_agentkit_plist_stays_parked_across_upgrades(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.install(with_agentkit=True)
    plist = fixture.agentkit_plist()
    parked_dir = fixture.home / install.PARKED_DIR
    parked_dir.mkdir(parents=True, exist_ok=True)
    parked = parked_dir / plist.name
    plist.replace(parked)
    fixture.install(with_agentkit=True)
    assert not plist.exists()
    assert parked.is_file()
