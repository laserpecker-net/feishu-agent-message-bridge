"""验收脚本的判定逻辑（纯函数部分）：布局是否"重启安全"、release 是否与 receipt 一致。"""

import json
import plistlib

from agentkit import rollout
from tools import verify_agentkit_rollout as verify


def _plist(directory, label):
    path = directory / (label + ".plist")
    path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/x", "y"]}))
    return path


def test_layout_is_reboot_safe_with_only_the_agentkit_plist(tmp_path, monkeypatch):
    service_dir = tmp_path / "LaunchAgents"
    service_dir.mkdir()
    monkeypatch.setattr(rollout, "_service_dir", lambda: service_dir)
    home = tmp_path / "home"
    home.mkdir()
    _plist(service_dir, rollout.AGENTKIT_LABEL)
    layout = verify.audit_layout(home)
    assert layout["active_consumer_plists"] == [rollout.AGENTKIT_LABEL]
    assert layout["retired_consumer_plists"] == []
    assert layout["reboot_safe"] is True


def test_a_leftover_retired_cursor_plist_is_not_reboot_safe(tmp_path, monkeypatch):
    """引擎 A 已删除：它的 plist 若还留在 LaunchAgents，登录时会拉起一个必然失败的进程。"""
    service_dir = tmp_path / "LaunchAgents"
    service_dir.mkdir()
    monkeypatch.setattr(rollout, "_service_dir", lambda: service_dir)
    home = tmp_path / "home"
    home.mkdir()
    _plist(service_dir, rollout.AGENTKIT_LABEL)
    _plist(service_dir, verify.RETIRED_CONSUMER_LABEL)
    layout = verify.audit_layout(home)
    assert layout["retired_consumer_plists"] == [verify.RETIRED_CONSUMER_LABEL]
    assert layout["reboot_safe"] is False


def test_parked_plists_are_reported_but_do_not_break_reboot_safety(tmp_path, monkeypatch):
    service_dir = tmp_path / "LaunchAgents"
    service_dir.mkdir()
    monkeypatch.setattr(rollout, "_service_dir", lambda: service_dir)
    home = tmp_path / "home"
    home.mkdir()
    _plist(service_dir, rollout.AGENTKIT_LABEL)
    (home / rollout.PARKED_DIR).mkdir()
    _plist(home / rollout.PARKED_DIR, rollout.AGENTKIT_LABEL)
    layout = verify.audit_layout(home)
    assert layout["parked_consumer_plists"] == [rollout.AGENTKIT_LABEL]
    assert layout["reboot_safe"] is True


def test_release_audit_reports_missing_receipt(tmp_path):
    report = verify._release_audit(tmp_path)
    assert report["ok"] is False and "installation.json" in report["reason"]


def test_release_audit_flags_a_tampered_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    release = home / "releases" / "abc"
    release.mkdir(parents=True)
    (release / "a.py").write_text("print('tampered')\n")
    receipt = {
        "agentkit": {
            "release": str(release),
            "release_files": {"a.py": "0" * 64},
        }
    }
    (home / "installation.json").write_text(json.dumps(receipt))
    report = verify._release_audit(home)
    assert report["ok"] is False
    assert any("a.py" in item for item in report["problems"])
